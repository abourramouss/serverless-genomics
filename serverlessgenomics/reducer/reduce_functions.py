from collections import defaultdict
import os
from subprocess import Popen, PIPE, STDOUT
from typing import Tuple
from time import time
from sys import getsizeof

from lithops import Storage
from ..pipeline import PipelineParameters

from ..stats import Stats


def reduce_function(keys, range, mpu_id, n_part, mpu_key, pipeline_params: PipelineParameters, storage: Storage):
    mainStat, timestamps, data_sizes = Stats(), Stats(), Stats()
    timestamps.store_size_data("start", time())
    mainStat.timer_start(f"reduce_{mpu_id}_{n_part}")
    s3 = storage.get_client()

    # Change working directory to /tmp
    wd = os.getcwd()
    os.chdir("/tmp")

    # S3 SELECT query to get the rows where the second column is in the selected range
    timestamps.store_size_data("s3_queries", time())
    expression = "SELECT * FROM s3object s WHERE cast(s._2 as int) BETWEEN %s AND %s" % (range["start"], range["end"])
    input_serialization = {"CSV": {"RecordDelimiter": "\n", "FieldDelimiter": "\t"}, "CompressionType": "NONE"}

    # Execute S3 SELECT
    mpileup_data = ""
    for k in keys:
        try:
            resp = s3.select_object_content(
                Bucket=pipeline_params.storage_bucket,
                Key=k,
                ExpressionType="SQL",
                Expression=expression,
                InputSerialization=input_serialization,
                OutputSerialization={"CSV": {"FieldDelimiter": "\t"}},
            )
        except:
            raise ValueError("ERROR IN KEY: " + k)

        for event in resp["Payload"]:
            if "Records" in event:
                records = event["Records"]["Payload"].decode("UTF-8")
                mpileup_data = mpileup_data + records
                del records

    data_sizes.store_size_data(
        f"total_data_from_select_range_{range['start']}_{range['end']}", getsizeof(mpileup_data) / (1024 * 1024)
    )

    # Str -> bytes
    mpileup_data = mpileup_data.encode("UTF-8")

    # Execute the script to merge and reduce
    timestamps.store_size_data("mpileup_merge_reduce", time())
    p = Popen(
        ["bash", "/function/bin/mpileup_merge_reducev3.sh", "/function/bin/", "75%"],
        stdout=PIPE,
        stdin=PIPE,
        stderr=PIPE,
    )
    sinple_out = p.communicate(input=mpileup_data)[0]
    sinple_out = sinple_out.decode("UTF-8")

    # Upload part
    timestamps.store_size_data("upload_part", time())
    part = s3.upload_part(
        Body=sinple_out, Bucket=pipeline_params.storage_bucket, Key=mpu_key, UploadId=mpu_id, PartNumber=n_part
    )
    data_sizes.store_size_data(f"part_{n_part}", getsizeof(sinple_out) / (1024 * 1024))
    data_sizes.store_size_data("keys", keys)

    timestamps.store_size_data("end", time())

    mainStat.timer_stop(f"reduce_{mpu_id}_{n_part}")
    mainStat.store_dictio(timestamps.get_stats(), "timestamps", f"reduce_{mpu_id}_{n_part}")
    mainStat.store_dictio(data_sizes.get_stats(), "data_sizes", f"reduce_{mpu_id}_{n_part}")

    return {"PartNumber": n_part, "ETag": part["ETag"], "mpu_id": mpu_id}, mainStat.get_stats()


def distribute_indexes(pipeline_params: PipelineParameters, keys: Tuple[str], storage: Storage) -> Tuple[Tuple[str]]:
    """
    Distribute the indexes between different reducers

    Args:
        pipeline_params (PipelineParameters): Pipeline Parameters
        keys (Tuple[str]): Keys to the files generated in the map phase
        storage (Storage): Lithops storage instance

    Returns:
        Tuple[Tuple[str]]: Array where each position consists of the list of keys a reducer should take
    """
    mainStat, timestamps, data_sizes = Stats(), Stats(), Stats()
    timestamps.store_size_data("start", time())
    mainStat.timer_start(f"distribute_indexes_{keys[0]}")
    s3 = storage.get_client()

    expression = "SELECT cast(s._2 as int) FROM s3object s"
    input_serialization = {"CSV": {"RecordDelimiter": "\n", "FieldDelimiter": "\t"}, "CompressionType": "NONE"}

    count_indexes = {}

    # First we get the number of times each index appears
    timestamps.store_size_data("s3_queries", time())
    for key in keys:
        resp = s3.select_object_content(
            Bucket=pipeline_params.storage_bucket,
            Key=key,
            ExpressionType="SQL",
            Expression=expression,
            InputSerialization=input_serialization,
            OutputSerialization={"CSV": {}},
        )

        data = ""
        for event in resp["Payload"]:
            if "Records" in event:
                records = event["Records"]["Payload"].decode("UTF-8")
                data = data + records
        data = data.split("\n")
        data.pop()  # Last value is empty

        int_indexes = list(map(int, data))

        for index in int_indexes:
            count_indexes[index] = count_indexes.get(index, 0) + 1

    data_sizes.store_size_data("total_data_from_select", getsizeof(data) / (1024 * 1024))
    data_sizes.store_size_data("keys", keys)

    # Now we distribute the indexes depending on the max number of indexes we want each reducer to process
    timestamps.store_size_data("distribute_indexes", time())
    MAX_INDEXES = 20_000_000
    workers_data = []
    indexes = 0

    for key in count_indexes:
        if indexes + count_indexes[key] < MAX_INDEXES:
            indexes += count_indexes[key]
            index = key
        else:  # append the last index below max_index as end value in range, and start a new range.
            indexes = 0
            workers_data.append(index)
    workers_data.append(key)

    timestamps.store_size_data("end", time())

    mainStat.timer_stop(f"distribute_indexes_{keys[0]}")
    mainStat.store_dictio(timestamps.get_stats(), "timestamps", f"distribute_indexes_{keys[0]}")
    mainStat.store_dictio(data_sizes.get_stats(), "data_sizes", f"distribute_indexes_{keys[0]}")

    return workers_data, mainStat.get_stats()


def final_merge(
    mpu_id: str, mpu_key: str, key: str, n_part: int, pipeline_params: PipelineParameters, storage: Storage
) -> dict:
    """
    Upload all the generated files by the reduce stage into one final file. This function will be mapped.

    Args:
        mpu_id (str): Multipart Upload ID
        mpu_key (str): Multipart Upload Key
        key (str): Key to the part file
        n_part (int): The number of the part file
        pipeline_params (PipelineParameters): Pipeline Parameters
        storage (Storage): Lithops storage instance

    Returns:
        dict: Dictionary with the multipart upload settings
    """
    stat = Stats()

    stat.timer_start(f"download_{key}")
    sinple_out = storage.get_object(bucket=pipeline_params.storage_bucket, key=key)
    stat.timer_stop(f"download_{key}")

    stat.timer_start(f"upload_{key}")
    # Upload part
    s3 = storage.get_client()
    part = s3.upload_part(
        Body=sinple_out, Bucket=pipeline_params.storage_bucket, Key=mpu_key, UploadId=mpu_id, PartNumber=n_part
    )
    stat.timer_stop(f"upload_{key}")

    return {"PartNumber": n_part, "ETag": part["ETag"], "mpu_id": mpu_id}, stat.get_stats()


def finish(key: str, mpu_id: str, parts: Tuple[dict], pipeline_params: PipelineParameters, s3: Storage):
    """
    Complete the final multipart upload

    Args:
        key (str): Multipart upload Key
        mpu_id (str): Multipart upload ID
        parts (Tuple[dict]): Multipart upload parts
        pipeline_params (PipelineParameters): Pipeline parameters
        s3 (Storage): Lithops storage instance
    """
    mpu_part = []
    remove = 0

    for part in parts:
        if mpu_id == part["mpu_id"]:
            mpu_part.append({"PartNumber": part["PartNumber"], "ETag": part["ETag"]})
            remove = remove + 1
        else:
            break

    s3.complete_multipart_upload(
        Bucket=pipeline_params.storage_bucket, Key=key, UploadId=mpu_id, MultipartUpload={"Parts": mpu_part}
    )


def complete_multipart(
    keys: Tuple[str], mpu_ids: Tuple[str], parts: Tuple[dict], pipeline_params: PipelineParameters, s3: Storage
):
    """
    Complete a list of multipart uploads.

    Args:
        keys (Tuple[str]): Keys to the multipart uploads
        mpu_ids (Tuple[str]): IDs to the multipart uploads
        parts (Tuple[dict]): Parts of each multipart upload
        pipeline_params (PipelineParameters): Pipeline Parameters
        s3 (Storage): Lithops storage instance
    """
    for key, mpu_id in zip(keys, mpu_ids):
        mpu_part = []
        remove = 0

        for part in parts:
            if mpu_id == part["mpu_id"]:
                mpu_part.append({"PartNumber": part["PartNumber"], "ETag": part["ETag"]})
                remove = remove + 1
            else:
                break

        s3.complete_multipart_upload(
            Bucket=pipeline_params.storage_bucket, Key=key, UploadId=mpu_id, MultipartUpload={"Parts": mpu_part}
        )

        parts = parts[remove:]


def keys_by_fasta_split(keys: Tuple[str]) -> dict:
    """
    Distribute the received keys by fasta split.

    Args:
        keys (Tuple[str]): List of keys

    Returns:
        dict: Dictionary where each key (0,1,2...) is the fasta split and the value is the list of keys
    """
    key_dict = defaultdict(list)
    for k in keys:
        fasta_split = k.split("/")[-1]
        fasta_split = fasta_split.split("fa")[-1]
        fasta_split = fasta_split[0]
        key_dict[str(fasta_split)].append(k)

    return key_dict


def create_multipart_keys(pipeline_params: PipelineParameters) -> Tuple[str]:
    """
    Create the keys that will be used for the multipart uploads

    Args:
        pipeline_params (PipelineParameters): Pipeline parameters

    Returns:
        Tuple[str]: List of keys
    """
    keys = []
    for i in range(pipeline_params.fasta_chunks):
        if (pipeline_params.fasta_chunk_range is None) or (i in pipeline_params.fasta_chunk_range):
            keys.append(f"tmp/{pipeline_params.run_id}/multipart_uploads/fa{i}.sinple")
    return keys


def create_multipart(pipeline_params: PipelineParameters, key: str, storage: Storage) -> str:
    """
    Create a S3 multipart upload instance

    Args:
        pipeline_params (PipelineParameters): Pipeline Parameters
        key (str): Key for the multipart upload
        storage (Storage): Lithops storage instance

    Returns:
        str: Multipart Upload ID
    """
    s3 = storage.get_client()
    mpu = s3.create_multipart_upload(Bucket=pipeline_params.storage_bucket, Key=key)
    return mpu["UploadId"]
