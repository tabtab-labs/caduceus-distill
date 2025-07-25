import os
import sys
import time
import traceback
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
import torch
import torch.nn as nn
import typer
import xarray as xr
from torch.cuda.amp import autocast
from torch.utils.data import DataLoader
from transformers import AutoModelForMaskedLM, AutoTokenizer, PreTrainedTokenizer
from upath import UPath

from caduceus_distill.data.hg38_dataset import HG38_EXAMPLE_T, HG38Dataset
from caduceus_distill.utils.utils import get_root_path, setup_basic_logging


def generate_soft_labels(
    *,
    bed_file: str | Path,
    fasta_file: str | Path,
    output_path: str | Path,
    split: str,
    seq_length: int = 2**17,
    batch_size: int = 1,
    device: Literal["cuda", "cpu"] = "cuda",
    max_batches: int | None = None,
) -> None:
    model_name: str = "kuleshov-group/caduceus-ps_seqlen-131k_d_model-256_n_layer-16"

    tokenizer: PreTrainedTokenizer = AutoTokenizer.from_pretrained(
        model_name, trust_remote_code=True
    )

    # Load the model initially to CPU. This is important for the sequential warm-up.
    model = AutoModelForMaskedLM.from_pretrained(model_name, trust_remote_code=True)

    assert model.training is False, "The model should be in eval mode for inference"

    if device == "cuda" and torch.cuda.is_available():
        num_gpus = torch.cuda.device_count()
        if num_gpus > 1:
            # === SEQUENTIAL WARM-UP ===
            # We warm up each GPU sequentially to prevent a race
            # condition in the Triton kernel autotuner when used with nn.DataParallel.
            print("Pre-warming Triton cache on all available GPUs sequentially...")
            for i in range(num_gpus):
                gpu_device = f"cuda:{i}"
                print(f"  Warming up on {gpu_device}...")
                try:
                    # Move model to the target GPU
                    model.to(gpu_device)

                    # Create a dummy input on the target GPU
                    dummy_input = torch.randint(
                        0,
                        tokenizer.vocab_size,
                        (1, seq_length),
                        dtype=torch.long,
                        device=gpu_device,
                    )

                    # Run a single forward pass to trigger JIT compilation
                    with torch.inference_mode(), autocast(enabled=True):
                        _ = model(dummy_input)

                    # Wait for all kernels to finish
                    torch.cuda.synchronize(gpu_device)
                    print(f"  Warm-up on {gpu_device} complete.")

                except Exception as e:
                    print(f"  An error occurred during warm-up on {gpu_device}: {e}")
                    print("  Attempting to proceed, but errors may occur.")

            print("All GPUs warmed up.")

        # Now, move the model to the primary device for DataParallel
        model.to(device)
        if num_gpus > 1:
            print(f"Using {num_gpus} GPUs with DataParallel!")
            model = nn.DataParallel(model)

    elif device == "cpu":
        model = model.to(device)

    dataset: HG38Dataset = HG38Dataset(
        split=split,
        bed_file=bed_file,
        fasta_file=fasta_file,
        seq_length=seq_length,
        tokenizer=tokenizer,
    )

    num_workers: int = min(os.cpu_count() or 1, 8)
    dataloader: DataLoader[HG38_EXAMPLE_T] = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=device == "cuda",
    )

    # Initialize NVML for GPU monitoring
    nvml_available: bool = False
    handles: list[Any] = []
    pynvml: Any = None
    device_count: int = 0

    if device == "cuda" and torch.cuda.is_available():
        try:
            import pynvml  # type: ignore[no-redef]

            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            handles = [
                pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(device_count)
            ]
            nvml_available = True
        except (ImportError, Exception) as e:
            print(
                f"Warning: pynvml library not found or NVIDIA driver/NVML issue: {e}. "
                "GPU metrics will not be reported."
            )

    # Initialize tracking variables
    last_print_time: float = time.time()
    total_tokens: int = 0
    tokens_since_last_print: int = 0
    total_batches: int = len(dataloader)
    start_time: float = time.time()
    processed_batches: int = 0
    was_interrupted: bool = False

    # Initialize variables for aggregating GPU stats
    sum_gpu_core_util: list[int] = [0] * device_count
    sum_gpu_mem_io_util: list[int] = [0] * device_count
    gpu_samples_count: int = 0

    try:
        for batch_idx, (input_ids, chr_names, starts, ends) in enumerate(dataloader):
            if max_batches is not None and batch_idx >= max_batches:
                break
            processed_batches = batch_idx + 1
            input_ids = input_ids.to(device)
            batch_tokens: int = input_ids.numel()
            tokens_since_last_print += batch_tokens
            total_tokens += batch_tokens

            with torch.inference_mode(), autocast(enabled=(device == "cuda")):
                outputs = model(input_ids)
                logits = outputs.logits

            if nvml_available and pynvml is not None:
                try:
                    for i, handle in enumerate(handles):
                        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                        sum_gpu_core_util[i] += util.gpu
                        sum_gpu_mem_io_util[i] += util.memory
                    gpu_samples_count += 1
                except Exception:
                    pass

            num_samples_in_batch: int = input_ids.shape[0]
            start_sample_idx: int = batch_idx * batch_size
            batch_indices: list[int] = list(
                range(start_sample_idx, start_sample_idx + num_samples_in_batch)
            )

            ds: xr.Dataset = xr.Dataset(
                {
                    "input_ids": (["sample", "sequence"], input_ids.cpu().numpy()),
                    "logits": (["sample", "sequence", "vocab"], logits.cpu().numpy()),
                },
                coords={
                    "sample": batch_indices,
                    "sequence": range(input_ids.shape[1]),
                    "vocab": range(logits.shape[-1]),
                    # NOTE: use U5 encoding because chr name can be either 3 or 4 characters long, e.g. `chr1` or `chr10`
                    "chr_name": (["sample"], np.asarray(list(chr_names)).astype("<U5")),
                    "start": (["sample"], [int(s) for s in starts]),
                    "end": (["sample"], [int(e) for e in ends]),
                },
            )

            zarr_kwargs: dict[str, Any] = {"zarr_format": 2, "consolidated": True}
            if UPath(output_path).exists():
                zarr_kwargs["append_dim"] = "sample"

            # NOTE: use single chunk per batch: https://github.com/pydata/xarray/discussions/6697#discussioncomment-2946370
            for var in ds.variables:
                ds[var].encoding["chunks"] = ds[var].shape

            ds.to_zarr(output_path, **zarr_kwargs)

            current_time: float = time.time()
            if current_time - last_print_time >= 5:
                elapsed_since_last: float = current_time - last_print_time
                tokens_per_second: float = tokens_since_last_print / elapsed_since_last
                progress_percent: float = (batch_idx + 1) / total_batches * 100

                gpu_metrics_str: str = ""
                if nvml_available and pynvml is not None and handles:
                    try:
                        metrics_parts = []
                        for i, handle in enumerate(handles):
                            avg_gpu_core = (
                                sum_gpu_core_util[i] / gpu_samples_count
                                if gpu_samples_count > 0
                                else 0
                            )
                            avg_gpu_mem_io = (
                                sum_gpu_mem_io_util[i] / gpu_samples_count
                                if gpu_samples_count > 0
                                else 0
                            )
                            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                            mem_used_mib = mem_info.used // (1024**2)
                            mem_total_mib = mem_info.total // (1024**2)

                            metrics_parts.append(
                                f"GPU{i}: Core {avg_gpu_core:.0f}%, "
                                f"MemIO {avg_gpu_mem_io:.0f}%, "
                                f"VRAM {mem_used_mib}/{mem_total_mib} MiB"
                            )
                        gpu_metrics_str = " | " + " | ".join(metrics_parts)
                    except Exception:
                        gpu_metrics_str = " | GPU Metrics: N/A"

                print(
                    f"Progress: {progress_percent:.2f}% ({batch_idx + 1}/{total_batches} batches) | "
                    f"Speed: {tokens_per_second:.2f} tokens/sec{gpu_metrics_str}"
                )

                last_print_time = current_time
                tokens_since_last_print = 0
                sum_gpu_core_util = [0] * device_count
                sum_gpu_mem_io_util = [0] * device_count
                gpu_samples_count = 0

    except KeyboardInterrupt:
        was_interrupted = True
        print("\nInterrupted by user!")

    except Exception as e:
        was_interrupted = True
        print(f"\n{traceback.format_exc()}\nError occurred: {str(e)}", file=sys.stderr)

    finally:
        if nvml_available and pynvml is not None:
            pynvml.nvmlShutdown()

        total_elapsed: float = time.time() - start_time

        if processed_batches == 0:
            print("No data was processed!")
        else:
            progress_percent = processed_batches / total_batches * 100
            completion_status: str = (
                "Partial completion" if was_interrupted else "Completed"
            )

            print(
                f"\n{completion_status}: {progress_percent:.2f}% ({processed_batches}/{total_batches} batches) | "
                f"Total tokens processed: {total_tokens} | "
                f"Average speed: {total_tokens/total_elapsed:.2f} tokens/sec | "
                f"Total time: {total_elapsed:.2f}s"
            )


def main(
    output_path: Annotated[str, typer.Argument(help="Output Zarr file path")],
    fasta_file: Annotated[
        str,
        typer.Option(
            help="Path to FASTA file",
        ),
    ] = str(get_root_path().joinpath("data", "hg38", "hg38.ml.fa")),
    bed_file: Annotated[
        str,
        typer.Option(
            help="Path to BED file",
        ),
    ] = str(get_root_path().joinpath("data", "hg38", "human-sequences.bed")),
    split: Annotated[
        str,
        typer.Option(
            help="hg38 dataset split (i.e., 'train', 'valid', 'test')",
        ),
    ] = "train",
    seq_length: Annotated[
        int,
        typer.Option(
            help=f"Chunk size for sequences (default: {2**17})",
        ),
    ] = 2
    ** 17,
    batch_size: Annotated[
        int,
        typer.Option(
            help="Batch size (default: 4)",
        ),
    ] = 4,
    device: Annotated[
        str,
        typer.Option(
            help="Device to use, either 'cuda' or 'cpu' (default: 'cuda')",
        ),
    ] = "cuda",
    max_batches: Annotated[
        int | None,
        typer.Option(
            help="Maximum number of batches to process (default: all batches)",
        ),
    ] = None,
) -> None:
    # To see a benefit from multiple GPUs, the batch size must be >= number of GPUs.
    if device == "cuda" and torch.cuda.is_available():
        num_gpus = torch.cuda.device_count()
        if num_gpus > 1 and batch_size < num_gpus:
            print(
                f"Warning: Batch size ({batch_size}) is less than the number of available GPUs ({num_gpus})."
            )
            print(
                f"To fully utilize all GPUs, increase batch size to be at least {num_gpus}."
            )

    generate_soft_labels(
        bed_file=bed_file,
        fasta_file=fasta_file,
        output_path=output_path,
        split=split,
        seq_length=seq_length,
        batch_size=batch_size,
        device=device,  # type: ignore[arg-type]
        max_batches=max_batches,
    )


if __name__ == "__main__":
    setup_basic_logging()
    typer.run(main)
