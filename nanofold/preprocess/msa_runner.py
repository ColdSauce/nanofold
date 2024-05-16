import gzip
import logging
import os
import subprocess
from pathlib import Path

from nanofold.preprocess.sto_parser import truncate_sto


class MSARunner:
    def __init__(self, jackhmmer_bin, small_bfd, cache_dir, num_cpus, max_sequences):
        self.jackhmmer_bin = jackhmmer_bin
        self.small_bfd = small_bfd
        self.cache_dir = cache_dir
        self.num_cpus = num_cpus
        self.max_sequences = max_sequences

    def build_jackhmmer_cmd(self, input, output):
        return [
            self.jackhmmer_bin,
            "--noali",
            "--cpu",
            str(self.num_cpus),
            "-A",
            output,
            "-N",
            "1",
            "-E",
            "0.0001",
            "--incE",
            "0.0001",
            "--F1",
            "0.0005",
            "--F2",
            "0.00005",
            "--F3",
            "0.0000005",
            input,
            str(self.small_bfd),
        ]

    def cached_result(self, output):
        zip_output = Path(f"{output}.gz")

        def zip_result():
            with gzip.open(zip_output, "rt") as gz_f:
                for line in gz_f:
                    yield line

        if zip_output.exists():
            return zip_result

        if output.exists() and os.path.getsize(output) > 0:
            with open(output) as f:
                content = f.read()
                with gzip.open(zip_output, "wb") as gz_f:
                    gz_f.write(content.encode())
            os.remove(output)
            return lambda: content
        return None

    def truncate_sto(self, output):
        if self.max_sequences is not None:
            with open(output, mode="r+") as f:
                contents = truncate_sto(f, self.max_sequences)
                f.seek(0)
                f.write(contents)
                f.truncate()

    def run(self, fasta_input, id):
        output = self.cache_dir / f"{id}.sto"
        tmp_output = self.cache_dir / f"{id}.sto.tmp"

        cached_result = self.cached_result(output)
        if cached_result is not None:
            return cached_result
        cmd = self.build_jackhmmer_cmd(fasta_input, tmp_output)
        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            logging.error(e.stderr.decode("utf-8"))
            raise e

        self.truncate_sto(tmp_output)
        os.rename(tmp_output, output)
        return self.cached_result(output)