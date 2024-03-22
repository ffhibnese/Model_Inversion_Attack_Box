from typing import Callable

import torch
from tqdm import tqdm

from .io import print_split_line

def _is_namedtuple(obj):
    # Check if type was created from collections.namedtuple or a typing.NamedTuple.
    return (
        isinstance(obj, tuple) and hasattr(obj, "_asdict") and hasattr(obj, "_fields")
    )

def _gather(outputs, dim=0):
    def gather_map(outputs):
        out = outputs[0]
        if isinstance(out, torch.Tensor):
            return torch.cat(outputs, dim=dim)
        if out is None:
            return None
        if isinstance(out, dict):
            if not all(len(out) == len(d) for d in outputs):
                raise ValueError('All dicts must have the same number of keys')
            return type(out)((k, gather_map([d[k] for d in outputs]))
                             for k in out)
        if _is_namedtuple(out):
            return type(out)._make(map(gather_map, zip(*outputs)))
        return type(out)(map(gather_map, zip(*outputs)))

    # Recursive function calls like this create reference cycles.
    # Setting the function to None clears the refcycle.
    try:
        res = gather_map(outputs)
    finally:
        gather_map = None
    return res

def batch_apply(fn: Callable, *inputs, batch_size: int, description: str = None, use_tqdm: bool=False):
    
    def _check_valid(inputs):
        if len(inputs) == 0:
            return
        lens = []
        for inp in inputs:
            try:
                lens.append(len(inp))
            except:
                raise RuntimeError('some inputs have no attr `len`')
        valid_len = lens[0]
        if not all(map(lambda x: x == valid_len, lens)):
            raise RuntimeError('lengths of all inputs are not the same')
    
    _check_valid(inputs)
    
    total_len = len(inputs[0])
    
    results = []
    starts = list(range(0, total_len, batch_size))
    iter_times = len(starts)
    
    if use_tqdm:
        starts = tqdm(starts, leave=False)
    
    for i, start in enumerate(starts, start=1):
        
        if description is not None:
            print_split_line(f'{description}: {i} / {iter_times}')
        
        end = min(total_len, start + batch_size)
        res = fn(*[p[start:end] for p in inputs])
        results.append(res)
    return _gather(results)