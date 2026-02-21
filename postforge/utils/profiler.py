# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostForge Profiling System

A modular profiling framework that can be easily enabled/disabled and extended
with different profiling backends (cProfile, line_profiler, memory_profiler, etc.)

Usage:
    # Command line
    postforge.bat --profile test.ps
    postforge.bat --profile-type=cprofile --profile-output=results.prof test.ps
    
    # In code
    with profiler.profile_context():
        # Code to profile
        pass
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


class ProfilerBackend(ABC):
    """Abstract base class for profiler backends"""
    
    def __init__(self, output_path: str | None = None) -> None:
        self.output_path = output_path
        self.enabled = True

    @abstractmethod
    def start_profiling(self) -> None:
        """Start profiling session"""
        pass

    @abstractmethod
    def stop_profiling(self) -> None:
        """Stop profiling session"""
        pass

    @abstractmethod
    def generate_report(self) -> str:
        """Generate human-readable report"""
        pass

    @abstractmethod
    def save_results(self) -> None:
        """Save profiling results to file"""
        pass


class CProfileBackend(ProfilerBackend):
    """cProfile-based profiling backend"""
    
    def __init__(self, output_path: str | None = None) -> None:
        super().__init__(output_path)
        self.profiler = cProfile.Profile()
        self.stats: pstats.Stats | None = None

    def start_profiling(self) -> None:
        """Start cProfile profiling"""
        if self.enabled:
            self.profiler.enable()
            
    def stop_profiling(self) -> None:
        """Stop cProfile profiling"""
        if self.enabled:
            self.profiler.disable()
            self.stats = pstats.Stats(self.profiler)
            
    def generate_report(self) -> str:
        """Generate cProfile report"""
        if not self.stats:
            return "No profiling data available"
            
        # Capture stats output
        s = io.StringIO()
        self.stats.print_stats(30)  # Top 30 functions
        return s.getvalue()
        
    def get_hotspots(self, limit: int = 10) -> list[tuple[Any, ...]]:
        """Get top hotspot functions"""
        if not self.stats:
            return []
            
        # Sort by cumulative time and get top functions
        self.stats.sort_stats('cumulative')
        return self.stats.get_stats_profile().func_profiles.items()[:limit]
        
    def save_results(self) -> None:
        """Save cProfile results to file"""
        if not self.stats or not self.output_path:
            return
            
        # Save binary stats file
        self.stats.dump_stats(self.output_path)
        
        # Also save human-readable report
        report_path = self.output_path.replace('.prof', '_report.txt')
        with open(report_path, 'w') as f:
            # Redirect stdout to capture print_stats output
            old_stdout = sys.stdout
            sys.stdout = f
            
            f.write("PostForge Performance Profiling Report\n")
            f.write("=" * 50 + "\n\n")
            
            f.write("Top 30 functions by cumulative time:\n")
            f.write("-" * 40 + "\n")
            self.stats.sort_stats('cumulative').print_stats(30)
            
            f.write("\n\nTop 20 functions by total time:\n")  
            f.write("-" * 35 + "\n")
            self.stats.sort_stats('tottime').print_stats(20)
            
            f.write("\n\nPostForge-specific functions:\n")
            f.write("-" * 30 + "\n")
            self.stats.print_stats('exec_exec|operators|control|types')
            
            sys.stdout = old_stdout


class NoOpBackend(ProfilerBackend):
    """No-operation profiler for when profiling is disabled"""
    
    def __init__(self, output_path: str | None = None) -> None:
        super().__init__(output_path)
        self.enabled = False

    def start_profiling(self) -> None:
        """No-op: profiling is disabled."""
        pass

    def stop_profiling(self) -> None:
        """No-op: profiling is disabled."""
        pass

    def generate_report(self) -> str:
        """Return a message indicating profiling is disabled."""
        return "Profiling disabled"

    def save_results(self) -> None:
        """No-op: profiling is disabled."""
        pass


class PostForgeProfiler:
    """Main profiler class that manages different profiling backends"""
    
    BACKEND_TYPES = {
        'cprofile': CProfileBackend,
        'none': NoOpBackend,
        # Future extensions:
        # 'line': LineProfileBackend,
        # 'memory': MemoryProfileBackend,
        # 'py-spy': PySpyBackend,
    }
    
    def __init__(self,
                 backend_type: str = 'none',
                 output_path: str | None = None,
                 enabled: bool = False) -> None:
        self.backend_type = backend_type
        self.output_path = output_path
        self.enabled = enabled
        self.backend = self._create_backend()
        self.session_stats: dict[str, Any] = {}
        
    def _create_backend(self) -> ProfilerBackend:
        """Create appropriate profiler backend"""
        if not self.enabled:
            return NoOpBackend(self.output_path)
            
        backend_class = self.BACKEND_TYPES.get(self.backend_type, NoOpBackend)
        return backend_class(self.output_path)
        
    @contextmanager
    def profile_context(self) -> Generator[PostForgeProfiler, None, None]:
        """Context manager for profiling a code block"""
        try:
            self.start()
            yield self
        finally:
            self.stop()
            
    def start(self) -> None:
        """Start profiling session"""
        if self.enabled:
            print(f"Starting {self.backend_type} profiling...")
            self.backend.start_profiling()
            
    def stop(self) -> None:
        """Stop profiling session"""
        if self.enabled:
            self.backend.stop_profiling()
            print(f"Profiling stopped. Backend: {self.backend_type}")
            
    def generate_report(self) -> str:
        """Generate profiling report"""
        return self.backend.generate_report()
        
    def save_results(self) -> None:
        """Save profiling results"""
        if self.enabled:
            self.backend.save_results()
            if self.output_path:
                print(f"Profiling results saved to: {self.output_path}")
                
    def print_summary(self) -> None:
        """Print a quick summary of profiling results"""
        if not self.enabled:
            print("Profiling was disabled")
            return
            
        print("\nProfiler Summary:")
        print("-" * 20)
        print(f"Backend: {self.backend_type}")
        print(f"Output: {self.output_path or 'None'}")
        
        if isinstance(self.backend, CProfileBackend) and self.backend.stats:
            # Get basic stats
            stats = self.backend.stats
            total_calls = stats.total_calls
            total_time = stats.total_tt
            
            print(f"Total function calls: {total_calls:,}")
            print(f"Total execution time: {total_time:.3f} seconds")
            
            # Show PostForge-specific functions
            print("\nPostForge hotspots:")
            postforge_stats = pstats.Stats(self.backend.profiler)
            postforge_stats.print_stats('exec_exec|operators|control|types', 5)
        else:
            print("No profiling statistics available")


# Global profiler instance - can be controlled via command line
_global_profiler: PostForgeProfiler | None = None


def initialize_profiler(backend_type: str = 'none',
                       output_path: str | None = None,
                       enabled: bool = False) -> PostForgeProfiler:
    """Initialize global profiler instance"""
    global _global_profiler
    _global_profiler = PostForgeProfiler(
        backend_type=backend_type,
        output_path=output_path, 
        enabled=enabled
    )
    return _global_profiler


def get_profiler() -> PostForgeProfiler:
    """Get global profiler instance"""
    global _global_profiler
    if _global_profiler is None:
        _global_profiler = PostForgeProfiler()  # Default disabled profiler
    return _global_profiler


@contextmanager
def profile_section(section_name: str = "") -> Generator[None, None, None]:
    """Convenience function for profiling code sections"""
    profiler = get_profiler()
    if profiler.enabled:
        print(f"Profiling section: {section_name}")
    
    with profiler.profile_context():
        yield


def generate_default_output_path(backend_type: str) -> str:
    """Generate default output path for profiling results"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    extensions = {
        'cprofile': 'prof',
        'line': 'lprof',
        'memory': 'mprof'
    }
    
    ext = extensions.get(backend_type, 'prof')
    return f"postforge_profile_{timestamp}.{ext}"


# Decorator for easy profiling of functions
def profile_function(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to profile a specific function"""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        profiler = get_profiler()
        if profiler.enabled:
            with profiler.profile_context():
                return func(*args, **kwargs)
        else:
            return func(*args, **kwargs)
    return wrapper