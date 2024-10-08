import logging

import lithops

from .mapping.map_caller import run_full_alignment
from .preprocessing import (
    prepare_fastq_chunks,
    prepare_fasta_chunks,
    prepare_gem_chunks,
)
from .reducer.reduce_caller import run_reducer

from .pipeline import PipelineParameters, PipelineRun, Lithops, validate_parameters, new_pipeline_run
from .stats import Stats
from .utils import setup_logging, log_parameters
from .lithopswrapper import LithopsInvokerWrapper

logger = logging.getLogger(__name__)


class VariantCallingPipeline:
    def __init__(self, **parameters):
        self.parameters: PipelineParameters = validate_parameters(parameters)
        setup_logging(self.parameters.log_level)

        logger.info("Init Serverless Variant Calling Pipeline")
        self.state: PipelineRun = new_pipeline_run(self.parameters)

        self._setup()

    def _setup(self):
        if self.parameters.log_level == logging.DEBUG:
            log_parameters(self.parameters)

        storage = lithops.storage.Storage()
        invoker = LithopsInvokerWrapper(self.parameters.lithops_settings)
        self.lithops = Lithops(storage=storage, invoker=invoker)

    @classmethod
    def restore_run(cls, run_id: str):
        # TODO
        raise NotImplementedError()

    def preprocess(self):
        """
        Prepare requested input data for alignment
        """
        preprocessStat = Stats()
        preprocessStat.timer_start("preprocess")

        fastq_chunks, subStatFastq = prepare_fastq_chunks(self.parameters, self.lithops)
        self.state.fastq_chunks = fastq_chunks

        fasta_chunks, subStatFasta = prepare_fasta_chunks(self.parameters, self.lithops)
        self.state.fasta_chunks = fasta_chunks

        self.state.gem_chunk_ids = prepare_gem_chunks(self.parameters, self.state.fasta_chunks, self.lithops)

        # subStat.timer_stop("gem_generator")
        # subStat.store_dictio(timers, "function_details", "gem_generator")
        preprocessStat.timer_stop("preprocess")
        preprocessStat.store_dictio(subStatFastq.get_stats(), "subprocesses_fastq", "preprocess")
        preprocessStat.store_dictio(subStatFasta.get_stats(), "subprocesses_fasta", "preprocess")
        return preprocessStat

    def alignment(self):
        """
        Alignment map pipeline step
        """
        # alignReadsStat = Stats()
        # alignReadsStat.timer_start("align_reads")
        run_full_alignment(self.parameters, self.state, self.lithops)
        # alignReadsStat.timer_stop("align_reads")
        # alignReadsStat.store_dictio(subStat.get_stats(), "phases", "align_reads")
        # return mapper_output, alignReadsStat

    def reduce(self, mapper_output):
        reduceStat = Stats()
        reduceStat.timer_start("reduce")
        subStat = run_reducer(self.parameters, self.lithops, mapper_output)
        reduceStat.timer_stop("reduce")
        reduceStat.store_dictio(subStat.get_stats(), "phases", "reduce")
        return reduceStat

    def pipeline_stats(self):
        stats, params = Stats(), Stats()

        stats.store_size_data("fasta_path", str(self.parameters.fasta_path))
        stats.store_size_data("fastq_path", str(self.parameters.fastq_path))
        stats.store_size_data("fastq_chunks", self.parameters.fastq_chunks)
        stats.store_size_data("fasta_chunks", self.parameters.fasta_chunks)
        stats.store_size_data("run_id", str(self.parameters.run_id))
        if self.parameters.fastq_chunk_range is not None:
            stats.store_size_data("fastq_range", str(self.parameters.fastq_chunk_range))

        stats.store_dictio(params.get_stats(), "pipeline_params")
        return stats

    def run_pipeline(self):
        """
        Execute all pipeline steps in order
        """
        self.preprocess()
        self.alignment()
        # self.reduce()
        # stats: Stats = self.pipeline_stats()
        # stats.timer_start("pipeline")

        # PreProcess Stage
        # if self.parameters.skip_prep is False:
        #     preprocessStat = self.preprocess()
        #
        # # Map Stage
        # if self.parameters.skip_map is False:
        #     mapper_output, alignReadsStat = self.mapping_alignment()
        #
        # # Reduce Stage
        # # TODO: If map phase was skipped an alternative mapper_ouput needs to be provided or generated
        # if self.parameters.skip_reduce is False:
        #     reduceStat = self.reduce(mapper_output)
        #
        # stats.timer_stop("pipeline")
        #
        # if self.parameters.skip_prep is False:
        #     stats.store_dictio(preprocessStat.get_stats(), "preprocess_phase", "pipeline")
        # if self.parameters.skip_map is False:
        #     stats.store_dictio(alignReadsStat.get_stats(), "alignReads_phase", "pipeline")
        # if self.parameters.skip_reduce is False:
        #     stats.store_dictio(reduceStat.get_stats(), "reduce_phase", "pipeline")
        #
        # if self.parameters.log_stats:
        #     stats.load_stats_to_json(self.parameters.storage_bucket, self.parameters.log_stats_name)

    def clean_all(self):
        logger.info("Going to delete all FASTQGZ Indexes")
        keys = self.lithops.storage.list_keys(
            self.parameters.storage_bucket, prefix=self.parameters.fastqgz_idx_prefix
        )
        self.lithops.storage.delete_objects(self.parameters.storage_bucket, keys)

        logger.info("Going to delete all FAIDX Indexes")
        keys = self.lithops.storage.list_keys(self.parameters.storage_bucket, prefix=self.parameters.faidx_prefix)
        self.lithops.storage.delete_objects(self.parameters.storage_bucket, keys)

        logger.info("Going to delete all GEM Indexes")
        keys = self.lithops.storage.list_keys(self.parameters.storage_bucket, prefix=self.parameters.gem_index_prefix)
        self.lithops.storage.delete_objects(self.parameters.storage_bucket, keys)

        # keys = self.lithops.storage.list_keys(self.parameters.storage_bucket, prefix=self.parameters.tmp_prefix)
        # self.lithops.storage.delete_objects(self.parameters.storage_bucket, keys)
