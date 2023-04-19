import bz2
import logging
import os
import multiprocessing
import pathlib
import shutil
import subprocess as sp
import tempfile
from typing import Tuple
import pandas as pd
from numpy import int64
from pathlib import PurePosixPath
from time import time

from ..datasource import fetch_fasta_chunk, fetch_fastq_chunk
from ..utils import force_delete_local_path
from ..pipeline import PipelineParameters
from lithops import Storage
from lithops.storage.utils import StorageNoSuchKeyError
import zipfile

from ..stats import Stats

logger = logging.getLogger(__name__)


def gem_indexer(pipeline_params: PipelineParameters, fasta_chunk_id: int, fasta_chunk: dict, storage: Storage):
    # Stats
    stat, timestamps, data_size = Stats(), Stats(), Stats()
    stat.timer_start(fasta_chunk_id)
    timestamps.store_size_data("start", time())

    # Initialize names
    gem_index_filename = os.path.join(f"{fasta_chunk_id}.gem")

    # Check if gem file already exists
    try:
        storage.head_object(bucket=pipeline_params.storage_bucket, key=f"gem/{gem_index_filename}")
        stat.timer_stop(fasta_chunk_id)
        return stat.get_stats()
    except StorageNoSuchKeyError:
        pass

    # Make temp dir and ch into it, save pwd to restore it later
    tmp_dir = tempfile.mkdtemp()
    pwd = os.getcwd()
    os.chdir(tmp_dir)

    try:
        # Get fasta chunk and store it to disk in tmp directory
        timestamps.store_size_data("download_fasta", time())
        fasta_chunk_filename = f"chunk_{fasta_chunk['chunk_id']}.fasta"
        fetch_fasta_chunk(fasta_chunk, fasta_chunk_filename, storage, pipeline_params.fasta_path)
        data_size.store_size_data(fasta_chunk_filename, os.path.getsize(fasta_chunk_filename) / (1024 * 1024))

        # gem-indexer appends .gem to output file
        timestamps.store_size_data("gem_indexer", time())
        cmd = [
            "gem-indexer",
            "--input",
            fasta_chunk_filename,
            "--threads",
            str(multiprocessing.cpu_count()),
            "-o",
            gem_index_filename.replace(".gem", ""),
        ]
        print(" ".join(cmd))
        out = sp.run(cmd, capture_output=True)
        print(out.stderr.decode("utf-8"))

        # TODO apparently 1 is return code for success (why)
        assert out.returncode == 1

        # Upload the gem file to storage
        timestamps.store_size_data("upload_gem", time())
        storage.upload_file(
            file_name=gem_index_filename, bucket=pipeline_params.storage_bucket, key=f"gem/{gem_index_filename}"
        )

        # Finish stats
        data_size.store_size_data(gem_index_filename, os.path.getsize(gem_index_filename) / (1024 * 1024))
        stat.timer_stop(fasta_chunk_id)
        timestamps.store_size_data("end", time())

        # Store dict
        stat.store_dictio(timestamps.get_stats(), "timestamps", fasta_chunk_id)
        stat.store_dictio(data_size.get_stats(), "data_sizes", fasta_chunk_id)
        return stat.get_stats()
    finally:
        os.chdir(pwd)
        force_delete_local_path(tmp_dir)


def align_mapper(
    pipeline_params: PipelineParameters,
    run_id: str,
    fasta_chunk_id: int,
    fasta_chunk: dict,
    fastq_chunk_id: int,
    fastq_chunk: dict,
    storage: Storage,
):
    """
    Lithops callee function
    First map function to filter and map fasta + fastq chunks. Some intermediate files
    are uploaded into the cloud storage for subsequent index correction, after which
    the final part of the map function (map_alignment2) can be executed.
    """
    stat, timestamps, data_size = Stats(), Stats(), Stats()
    timestamps.store_size_data("start", time())
    mapper_id = f"fa{fasta_chunk_id}-fq{fastq_chunk_id}"
    stat.timer_start(mapper_id)
    map_index_key = os.path.join(
        pipeline_params.tmp_prefix,
        run_id,
        "gem-mapper",
        f"fq{fastq_chunk_id}",
        f"fa{fasta_chunk_id}",
        pipeline_params.sra_accession + "_map.index.txt.bz2",
    )
    filtered_map_key = os.path.join(
        pipeline_params.tmp_prefix,
        run_id,
        "gem-mapper",
        f"fq{fastq_chunk_id}",
        f"fa{fasta_chunk_id}",
        pipeline_params.sra_accession + "_filt_wline_no.map.bz2",
    )

    # Check if output files already exist in storage
    try:
        storage.head_object(bucket=pipeline_params.storage_bucket, key=map_index_key)
        storage.head_object(bucket=pipeline_params.storage_bucket, key=filtered_map_key)
        # If they exist, return the keys and skip computing this chunk
        stat.timer_stop(mapper_id)
        return (fastq_chunk_id, fasta_chunk_id, map_index_key, filtered_map_key), stat.get_stats()
    except StorageNoSuchKeyError:
        # If any output is missing, proceed
        pass

    # Make temp dir and ch into it, save pwd to restore it later
    tmp_dir = tempfile.mkdtemp()
    pwd = os.getcwd()
    os.chdir(tmp_dir)
    print("Working directory: ", os.getcwd())
    try:
        # Get fastq chunk and store it to disk in tmp directory
        timestamps.store_size_data("download_fastq", time())
        fastq_chunk_filename = f"chunk_{fastq_chunk['chunk_id']}.fastq"
        fetch_fastq_chunk(fastq_chunk)

        data_size.store_size_data(fastq_chunk_filename, os.path.getsize(fastq_chunk_filename) / (1024 * 1024))

        # Get fasta chunk and store it to disk in tmp directory
        timestamps.store_size_data("download_fasta", time())
        fasta_chunk_filename = f"chunk_{fasta_chunk['chunk_id']}.fasta"
        fetch_fasta_chunk(fasta_chunk, fasta_chunk_filename, storage, pipeline_params.fasta_path)
        data_size.store_size_data(fasta_chunk_filename, os.path.getsize(fasta_chunk_filename) / (1024 * 1024))

        # Fetch gem file
        timestamps.store_size_data("download_gem", time())
        gem_index_filename = os.path.join(f"{fasta_chunk_id}.gem")
        storage.download_file(
            bucket=pipeline_params.storage_bucket, key=f"gem/{gem_index_filename}", file_name=gem_index_filename
        )
        data_size.store_size_data(gem_index_filename, os.path.getsize(gem_index_filename) / (1024 * 1024))

        # GENERATE ALIGNMENT AND ALIGNMENT INDEX (FASTQ TO MAP)
        # TODO refactor bash script
        # TODO support implement paired-end, replace not-used with 2nd fastq chunk
        # TODO use proper tmp directory instead of uuid base name

        timestamps.store_size_data("map_index_and_filter_map", time())
        # s3 or SRA works the same for the script /function/bin/map_index_and_filter_map_file_cmd_awsruntime.sh on single end sequences.
        cmd = [
            "/function/bin/map_index_and_filter_map_file_cmd_awsruntime.sh",
            gem_index_filename,
            fastq_chunk_filename,
            "not-used",
            pipeline_params.base_name,
            "s3",
            "single-end",
        ]
        print(" ".join(cmd))
        out = sp.run(cmd, capture_output=True)
        print(out.stdout.decode("utf-8"))

        # Reorganize file names
        map_index_filename = os.path.join(tmp_dir, pipeline_params.base_name + "_map.index.txt")
        shutil.move(pipeline_params.base_name + "_map.index.txt", map_index_filename)
        filtered_map_filename = os.path.join(
            tmp_dir, pipeline_params.base_name + "_" + str(mapper_id) + "_filt_wline_no.map"
        )
        shutil.move(pipeline_params.base_name + "_filt_wline_no.map", filtered_map_filename)

        # Compress outputs
        timestamps.store_size_data("compress_index", time())
        zipped_map_index_filename = map_index_filename + ".bz2"
        with zipfile.ZipFile(zipped_map_index_filename, "w", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
            zf.write(map_index_filename, arcname=PurePosixPath(map_index_filename).name)

        timestamps.store_size_data("compress_map", time())
        zipped_filtered_map_filename = filtered_map_filename + ".bz2"
        with zipfile.ZipFile(zipped_filtered_map_filename, "w", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
            zf.write(filtered_map_filename, arcname=PurePosixPath(filtered_map_filename).name)

        # Copy result files to storage for index correction
        timestamps.store_size_data("upload_index", time())
        storage.upload_file(
            file_name=zipped_map_index_filename, bucket=pipeline_params.storage_bucket, key=map_index_key
        )
        data_size.store_size_data(
            zipped_map_index_filename, os.path.getsize(zipped_map_index_filename) / (1024 * 1024)
        )

        timestamps.store_size_data("upload_map", time())
        storage.upload_file(
            file_name=zipped_filtered_map_filename, bucket=pipeline_params.storage_bucket, key=filtered_map_key
        )
        data_size.store_size_data(
            zipped_filtered_map_filename, os.path.getsize(zipped_filtered_map_filename) / (1024 * 1024)
        )

        timestamps.store_size_data("end", time())

        stat.timer_stop(mapper_id)
        stat.store_dictio(timestamps.get_stats(), "timestamps", mapper_id)
        stat.store_dictio(data_size.get_stats(), "data_sizes", mapper_id)
        return (fastq_chunk_id, fasta_chunk_id, map_index_key, filtered_map_key), stat.get_stats()
    finally:
        print("Cleaning up")


def index_correction(
    pipeline_params: PipelineParameters, fastq_chunk_id: int, map_index_keys: Tuple[str], storage: Storage
):
    """
    Lithops callee function
    Corrects the index after the first map iteration.
    All the set files must have the prefix "map_index_files/".
    Corrected indices will be stored with the prefix "corrected_index/".

    Args:
        setname (str): files to be corrected
        bucket (str): s3 bucket where the set is stored
        exec_param (str): string used to differentiate this pipeline execution from others with different parameters
        storage (Storage): s3 storage instance, generated by lithops
    """
    stat, timestamps, data_sizes = Stats(), Stats(), Stats()
    timestamps.store_size_data("start", time())
    set_name = f"fq_{fastq_chunk_id}"
    stat.timer_start(set_name)
    pwd = os.getcwd()

    # TODO replace with a proper file name (maybe indicating fastq chunk id)
    output_file = "merged_filtered_index.txt"
    zipped_output_file = output_file + ".bz2"
    corrected_index_key = os.path.join(
        pipeline_params.tmp_prefix,
        pipeline_params.run_id,
        "index-correction",
        f"fq{fastq_chunk_id}",
        zipped_output_file,
    )

    # Check if output files already exist in storage
    try:
        storage.head_object(bucket=pipeline_params.storage_bucket, key=corrected_index_key)
        stat.timer_stop(set_name)
        # If they exist, return the keys and skip computing this chunk
        return (fastq_chunk_id, corrected_index_key), stat.get_stats()
    except StorageNoSuchKeyError:
        # If the output is missing, proceed
        pass

    # Download all map files for this fastq chunk
    input_temp_dir = tempfile.mkdtemp()
    output_temp_dir = tempfile.mkdtemp()
    try:
        timestamps.store_size_data("download_indexes", time())
        for i, map_index_key in enumerate(map_index_keys):
            local_compressed_map_path = os.path.join(input_temp_dir, f"map_{i}.map.bz2")

            storage.download_file(
                bucket=pipeline_params.storage_bucket, key=map_index_key, file_name=local_compressed_map_path
            )

            with zipfile.ZipFile(local_compressed_map_path, "r", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
                zf.extractall(input_temp_dir)
            data_sizes.store_size_data(
                local_compressed_map_path, os.path.getsize(local_compressed_map_path) / (1024 * 1024)
            )
            # TODO set proper map index file name
            os.rename(
                os.path.join(input_temp_dir, f"{pipeline_params.base_name}_map.index.txt"),
                os.path.join(input_temp_dir, f"{i}_map.index.txt"),
            )
            os.remove(local_compressed_map_path)
        print(os.listdir(input_temp_dir))

        # TODO chdir required or binary_reducer.sh
        os.chdir(output_temp_dir)

        # Execute correction scripts
        timestamps.store_size_data("merge_gem", time())
        intermediate_file = f"{set_name}.intermediate.txt"
        # TODO binary_reducer.sh could be more efficiently implemented using python and consuming files from storage as a generator
        cmd = f"/function/bin/binary_reducer.sh /function/bin/merge_gem_alignment_metrics.sh 4 {input_temp_dir}/* > {intermediate_file}"
        print(cmd)
        proc = sp.run(cmd, shell=True, universal_newlines=True, capture_output=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        timestamps.store_size_data("filter_merged", time())
        cmd = f"/function/bin/filter_merged_index.sh {intermediate_file} {output_file}"
        print(cmd)
        proc = sp.run(cmd, shell=True, check=True, universal_newlines=True)
        print(proc.stdout)
        print(proc.stderr)
        proc.check_returncode()

        print(os.listdir())

        # Compress output
        timestamps.store_size_data("compress_corrected_index", time())
        with zipfile.ZipFile(zipped_output_file, "w", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
            zf.write(output_file, arcname=output_file)

        # Upload corrected index to storage
        timestamps.store_size_data("upload_corrected_index", time())
        storage.upload_file(
            bucket=pipeline_params.storage_bucket, key=corrected_index_key, file_name=zipped_output_file
        )
        data_sizes.store_size_data(zipped_output_file, os.path.getsize(zipped_output_file) / (1024 * 1024))

        timestamps.store_size_data("end", time())

        os.chdir(pwd)
        stat.timer_stop(set_name)
        stat.store_dictio(timestamps.get_stats(), "timestamps", set_name)
        stat.store_dictio(data_sizes.get_stats(), "data_sizes", set_name)
        return (fastq_chunk_id, corrected_index_key), stat.get_stats()
    finally:
        os.chdir(pwd)
        force_delete_local_path(input_temp_dir)
        force_delete_local_path(output_temp_dir)


def filter_index_to_mpileup(
    pipeline_params: PipelineParameters,
    fasta_chunk_id: int,
    fasta_chunk: dict,
    fastq_chunk_id: int,
    fastq_chunk: dict,
    filtered_map_key: str,
    corrected_index_key: str,
    storage: Storage,
):
    """
    Second map  function, executed after the previous map function (map_alignment1) and the index correction.

    Args:
        old_id (int): id used in the previous map function
        fasta_chunk (dict): contains all the necessary info related to the fasta chunk
        fastq_chunk (str): fastq chunk key
        corrected_map_index_file (str): key of the corrected index stored in s3
        filtered_map_file (str): key of the filtered map file generated in the previous map function and stored in s3
        base_name (str): name used in the name of some of the generated files
        exec_param (str): string used to differentiate this pipeline execution from others with different parameters
        storage (Storage): s3 storage instance, generated by lithops

    Returns:
        Tuple[str]: keys to the generated txt and csv/parquet files (stored in s3)
    """
    stat, timestamps, data_sizes = Stats(), Stats(), Stats()
    timestamps.store_size_data("start", time())
    stat.timer_start(f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}")
    temp_dir = tempfile.mkdtemp()
    pwd = os.getcwd()
    # TODO get base name from params

    corrected_map_file = (
        f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}_filt_wline_no_corrected.map"
    )
    mpileup_file = corrected_map_file + ".mpileup"
    mpipleup_key = os.path.join(
        pipeline_params.tmp_prefix,
        pipeline_params.run_id,
        "mpileups",
        f"fq{fastq_chunk_id}",
        f"fa{fasta_chunk_id}",
        mpileup_file,
    )

    # Check if output files already exist in storage
    try:
        storage.head_object(bucket=pipeline_params.storage_bucket, key=mpipleup_key)
        # If they exist, return the keys and skip computing this chunk
        stat.timer_stop(f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}")
        return mpipleup_key, stat.get_stats()

    except StorageNoSuchKeyError:
        # If the output is missing, proceed
        pass

    try:
        os.chdir(temp_dir)

        # Recover fasta chunk
        timestamps.store_size_data("download_fasta_chunk", time())
        fasta_chunk_filename = f"chunk_{fasta_chunk['chunk_id']}.fasta"
        fetch_fasta_chunk(fasta_chunk, fasta_chunk_filename, storage, pipeline_params.fasta_path)
        data_sizes.store_size_data(fasta_chunk_filename, os.path.getsize(fasta_chunk_filename) / (1024 * 1024))

        # Recover filtered map file
        timestamps.store_size_data("download_map_file", time())
        bz2_filt_map_filename = pathlib.PurePosixPath(filtered_map_key).name
        storage.download_file(
            bucket=pipeline_params.storage_bucket, key=filtered_map_key, file_name=bz2_filt_map_filename
        )
        data_sizes.store_size_data(bz2_filt_map_filename, os.path.getsize(bz2_filt_map_filename) / (1024 * 1024))
        with zipfile.ZipFile(bz2_filt_map_filename, "r", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
            zf.extractall()
        os.remove(bz2_filt_map_filename)

        # Get corrected map index for this fastq chunk
        try:
            bz2_corrected_index_filename = pathlib.PurePosixPath(corrected_index_key).name
        except:
            raise ValueError(corrected_index_key)
        timestamps.store_size_data("download_index", time())
        storage.download_file(
            bucket=pipeline_params.storage_bucket, key=corrected_index_key, file_name=bz2_corrected_index_filename
        )
        data_sizes.store_size_data(
            bz2_corrected_index_filename, os.path.getsize(bz2_corrected_index_filename) / (1024 * 1024)
        )
        with zipfile.ZipFile(bz2_corrected_index_filename, "r", compression=zipfile.ZIP_BZIP2, compresslevel=9) as zf:
            zf.extractall()
        os.remove(bz2_corrected_index_filename)

        print(os.listdir(temp_dir))

        # Filter aligments with corrected map file
        timestamps.store_size_data("map_file_index_correction", time())
        filt_map_filename = f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}_filt_wline_no.map"
        # TODO replace with a proper file name (maybe indicating fastq chunk id)
        corrected_index_filename = "merged_filtered_index.txt"

        cmd = [
            "/function/bin/map_file_index_correction.sh",
            corrected_index_filename,
            filt_map_filename,
            str(pipeline_params.tolerance),
        ]
        print(" ".join(cmd))
        proc = sp.run(cmd, capture_output=True)  # change to _v3.sh and runtime 20
        print(proc.stdout.decode("utf-8"))
        print(proc.stderr.decode("utf-8"))

        print(os.listdir(temp_dir))

        # Generate mpileup
        timestamps.store_size_data("gempileup_run", time())
        cmd = ["/function/bin/gempileup_run.sh", corrected_map_file, fasta_chunk_filename]
        print(" ".join(cmd))
        proc = sp.run(cmd, capture_output=True)
        print(proc.stdout.decode("utf-8"))
        print(proc.stderr.decode("utf-8"))

        # Store output to storage
        timestamps.store_size_data("upload_mpileup", time())
        mpipleup_key = os.path.join(
            pipeline_params.tmp_prefix,
            pipeline_params.run_id,
            "mpileups",
            f"fq{fastq_chunk_id}",
            f"fa{fasta_chunk_id}",
            mpileup_file,
        )
        storage.upload_file(
            bucket=pipeline_params.storage_bucket, key=mpipleup_key, file_name=f"{corrected_map_file}.mpileup"
        )
        data_sizes.store_size_data(
            f"{corrected_map_file}.mpileup", os.path.getsize(f"{corrected_map_file}.mpileup") / (1024 * 1024)
        )

        timestamps.store_size_data("end", time())

        stat.timer_stop(f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}")
        stat.store_dictio(
            timestamps.get_stats(), "timestamps", f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}"
        )
        stat.store_dictio(
            data_sizes.get_stats(), "data_sizes", f"{pipeline_params.base_name}_fa{fasta_chunk_id}-fq{fastq_chunk_id}"
        )
        return mpipleup_key, stat.get_stats()

    finally:
        os.chdir(pwd)
        force_delete_local_path(temp_dir)


def mpileup_conversion(
    self, mpileup_file: str, fasta_chunk: dict, fastq_chunk: str, exec_param: str, storage: Storage
) -> Tuple[str]:
    """
    Convert resulting data to csv/parquet and txt

    Args:
        mpileup_file (str): mpileup file
        fasta_chunk (dict): contains all the necessary info related to the fasta chunk
        fastq_chunk (str): fastq chunk key
        exec_param (str): string used to differentiate this pipeline execution from others with different parameters
        storage (Storage): s3 storage instance, generated by lithops

    Returns:
        Tuple[str]: keys to the generated txt and csv/parquet files (stored in s3)
    """

    # Filter mpileup file
    with open(mpileup_file, "r") as f:
        rows = f.read().splitlines()
        content = [row.split("\t") for row in rows]
        content.pop(-1)
        del rows

    # Convert mpileup to Pandas dataframe
    df = pd.DataFrame(data=content)
    df.columns = df.columns.astype(str)
    df["1"] = df["1"].astype(int64)

    # Remove disallowed characters
    fasta_key = self.fasta_chunks_prefix
    disallowed_characters = "._-!/·¡"
    for character in disallowed_characters:
        fasta_key = fasta_key.replace(character, "")

    # Create intermediate key
    fasta_chunk = str(fasta_chunk["id"])
    max_index = df.iloc[-1]["1"]
    intermediate_key = (
        self.args.file_format
        + "/"
        + exec_param
        + "/"
        + fasta_key
        + "_"
        + fasta_chunk
        + "-"
        + fastq_chunk[0]
        + "_chunk"
        + str(fastq_chunk[1]["number"])
        + "_"
        + str(max_index)
    )

    range_index = []
    x = 0
    while x < max_index:
        if x < max_index:
            range_index.append(x)
        x = x + 100000

    x = x + (max_index - x)
    range_index.append(x)

    df3 = df.groupby(pd.cut(df["1"], range_index)).count()
    content = ""
    for i in range(len(df3)):
        content = content + str(range_index[i + 1]) + ":" + str(df3.iloc[i, 0]) + "\n"

    # Upload .txt file to storage
    storage.put_object(bucket=self.args.storage_bucket, key=intermediate_key + ".txt", body=content)

    # Write the mpileup file to the tmp directory
    if self.args.file_format == "csv":
        df.to_csv(mpileup_file + ".csv", index=False, header=False)
        with open(mpileup_file + ".csv", "rb") as f:
            storage.put_object(bucket=self.args.storage_bucket, key=intermediate_key + ".csv", body=f)

    elif self.args.file_format == "parquet":
        df.to_parquet(mpileup_file + ".parquet")
        with open(mpileup_file + ".parquet", "rb") as f:
            storage.put_object(bucket=self.args.storage_bucket, key=intermediate_key + ".parquet", body=f)

    return [intermediate_key + "." + self.args.file_format, intermediate_key + ".txt"]
