# Profiling

PostForge includes a cProfile-based profiling system for identifying
performance bottlenecks. It also has memory analysis flags for tracking
allocation and garbage collection behavior.

## Performance Profiling

### Command Line Usage

```bash
./postforge.sh --profile script.ps
./postforge.sh --profile --profile-output results.prof script.ps
./postforge.sh --profile --profile-type cprofile script.ps
```

| Option | Description | Default |
|--------|-------------|---------|
| `--profile` | Enable performance profiling | Disabled |
| `--profile-type` | Profiling backend (`cprofile`, `none`) | `cprofile` |
| `--profile-output` | Output file path | Auto-generated with timestamp |

Profiling can be combined with other flags:

```bash
./postforge.sh --profile -d png script.ps
```

### Output

When profiling is enabled, two files are generated:

1. **Binary profile** (`.prof`) — raw cProfile data, compatible with Python's
   `pstats` module and third-party tools
2. **Text report** (`_report.txt`) — top functions by cumulative and total
   time, plus PostForge-specific function hotspots

### Analyzing Results

```bash
# Interactive analysis with pstats
python -m pstats results.prof

# Within the pstats shell:
# sort cumulative
# stats 20
# stats exec_exec
# callers exec_exec

# Visual flame graph with snakeviz
pip install snakeviz
snakeviz results.prof
```

### Programmatic Usage

The profiler can also be used from Python code:

```python
from postforge.utils import profiler as ps_profiler

profiler = ps_profiler.initialize_profiler(
    backend_type='cprofile',
    output_path='results.prof',
    enabled=True
)

with profiler.profile_context():
    # Code to profile
    pass

profiler.save_results()
profiler.print_summary()
```

## Memory Analysis

PostForge includes memory analysis flags for debugging allocation patterns.
These add overhead and are for development use only.

```bash
./postforge.sh --memory-profile script.ps    # Basic memory profiling
./postforge.sh --gc-analysis script.ps       # Garbage collection analysis
./postforge.sh --leak-analysis script.ps     # Detailed leak detection
```

| Flag | Description |
|------|-------------|
| `--memory-profile` | Basic memory usage reports |
| `--gc-analysis` | GC analysis (implies `--memory-profile`) |
| `--leak-analysis` | Memory leak detection (implies `--memory-profile`) |

## Key Files

| File | Purpose |
|------|---------|
| `postforge/utils/profiler.py` | Profiling framework (backends, context manager, CLI integration) |
| `postforge/utils/memory.py` | Memory analysis utilities |
