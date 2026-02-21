# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
PostScript Memory Profiling and Garbage Collection Analysis

This module provides comprehensive memory monitoring for PostForge including:
- Python process memory usage (RSS, VMS)
- PostScript VM memory tracking (lvm, gvm sizes)
- Python garbage collection monitoring  
- Object count tracking
- Memory leak detection
- Reference chain analysis

Key Features:
- Real-time memory monitoring during job execution
- Detailed GC statistics and timing
- PostScript VM size tracking
- Memory usage reporting and analysis
- Reference leak detection and analysis
"""

from __future__ import annotations

import gc
import os
import sys
import time
import tracemalloc
import weakref
from collections import Counter, defaultdict
from typing import Any

import psutil

from ..core import types as ps


class MemoryProfiler:
    """
    Comprehensive memory profiler for PostScript execution.
    
    Tracks Python process memory, PostScript VM memory, garbage collection
    activity, and provides detailed analysis and reporting.
    """
    
    def __init__(self, enable_tracemalloc: bool = True):
        """Initialize memory profiler with optional tracemalloc."""
        self.process = psutil.Process(os.getpid())
        self.enable_tracemalloc = enable_tracemalloc
        self.snapshots: list[dict[str, Any]] = []
        self.gc_stats: list[dict[str, Any]] = []
        self.start_time = time.perf_counter()
        
        # Reference tracking for leak detection
        self.object_registry: dict[int, weakref.ref] = {}
        self.reference_counts: dict[str, list[int]] = defaultdict(list)
        self.ps_object_lifecycle: dict[int, dict[str, Any]] = {}
        
        # Enable Python memory tracing
        if enable_tracemalloc:
            tracemalloc.start()
            
        # Set up GC callbacks
        self._setup_gc_monitoring()
        
        # Initial snapshot
        self.take_snapshot("startup")
    
    def _setup_gc_monitoring(self) -> None:
        """Set up garbage collection monitoring."""
        # Store original GC settings
        self.original_gc_thresholds = gc.get_threshold()
        
        # Enable GC debugging (optional - can be memory intensive)
        # gc.set_debug(gc.DEBUG_STATS)
        
    def take_snapshot(self, label: str, context: ps.Context | None = None) -> dict[str, Any]:
        """
        Take a comprehensive memory snapshot.
        
        Args:
            label: Description of when this snapshot was taken
            context: PostScript context (if available) for VM analysis
            
        Returns:
            Dictionary containing all memory metrics
        """
        timestamp = time.perf_counter() - self.start_time
        
        # Python process memory
        memory_info = self.process.memory_info()
        memory_percent = self.process.memory_percent()
        
        # Python object counts
        object_counts = self._get_object_counts()
        
        # Garbage collection stats
        gc_counts = gc.get_count()
        gc_stats = gc.get_stats()
        
        # PostScript VM analysis (if context provided)
        ps_vm_stats = self._analyze_ps_vm(context) if context else {}
        
        # Tracemalloc snapshot
        tracemalloc_stats = self._get_tracemalloc_stats() if self.enable_tracemalloc else {}
        
        snapshot = {
            'timestamp': timestamp,
            'label': label,
            'process_memory': {
                'rss_mb': memory_info.rss / 1024 / 1024,  # Resident Set Size
                'vms_mb': memory_info.vms / 1024 / 1024,  # Virtual Memory Size
                'percent': memory_percent,
                'available_mb': psutil.virtual_memory().available / 1024 / 1024
            },
            'python_objects': object_counts,
            'garbage_collection': {
                'counts': gc_counts,  # (gen0, gen1, gen2) pending collections
                'stats': gc_stats,    # Detailed GC statistics per generation
                'total_collections': sum(stat['collections'] for stat in gc_stats)
            },
            'postscript_vm': ps_vm_stats,
            'tracemalloc': tracemalloc_stats
        }
        
        self.snapshots.append(snapshot)
        return snapshot
    
    def _get_object_counts(self) -> dict[str, int]:
        """Get counts of different object types."""
        # Count all objects by type
        type_counts = {}
        for obj in gc.get_objects():
            obj_type = type(obj).__name__
            type_counts[obj_type] = type_counts.get(obj_type, 0) + 1
        
        # PostScript-specific object counts
        ps_counts = {}
        ps_types_to_track = ['Context', 'Array', 'Dict', 'String', 'Save', 'Int', 'Real', 'Bool']
        
        for obj in gc.get_objects():
            if hasattr(obj, '__class__') and obj.__class__.__module__ == 'ps_types':
                ps_type = obj.__class__.__name__
                if ps_type in ps_types_to_track:
                    ps_counts[f'ps_{ps_type}'] = ps_counts.get(f'ps_{ps_type}', 0) + 1
        
        # Track PostScript object lifecycle
        self._track_ps_object_lifecycle()
        
        return {
            'total_objects': len(gc.get_objects()),
            'by_type': dict(list(type_counts.items())[:20]),  # Top 20 types
            'postscript_objects': ps_counts,
            'reference_analysis': self._analyze_references()
        }
    
    def _track_ps_object_lifecycle(self) -> None:
        """Track PostScript object creation and references."""
        current_ps_objects = []
        
        for obj in gc.get_objects():
            if hasattr(obj, '__class__') and obj.__class__.__module__ == 'ps_types':
                obj_id = id(obj)
                obj_type = obj.__class__.__name__
                
                current_ps_objects.append(obj_id)
                
                # Track new objects
                if obj_id not in self.ps_object_lifecycle:
                    self.ps_object_lifecycle[obj_id] = {
                        'type': obj_type,
                        'created': time.perf_counter() - self.start_time,
                        'ref_count': sys.getrefcount(obj),
                        'referrers': len(gc.get_referrers(obj))
                    }
                else:
                    # Update existing object stats
                    self.ps_object_lifecycle[obj_id].update({
                        'ref_count': sys.getrefcount(obj),
                        'referrers': len(gc.get_referrers(obj))
                    })
        
        # Clean up lifecycle tracking for deleted objects
        to_remove = []
        for obj_id in self.ps_object_lifecycle:
            if obj_id not in current_ps_objects:
                to_remove.append(obj_id)
        
        for obj_id in to_remove:
            del self.ps_object_lifecycle[obj_id]
    
    def _analyze_references(self) -> dict[str, Any]:
        """Analyze object references to identify potential leaks."""
        try:
            # Get all objects
            all_objects = gc.get_objects()
            
            # Count references by type
            ref_counts = Counter()
            strong_refs = 0
            circular_refs = 0
            
            for obj in all_objects:
                obj_type = type(obj).__name__
                ref_count = sys.getrefcount(obj)
                ref_counts[obj_type] += ref_count
                
                if ref_count > 10:  # Arbitrary threshold for "many references"
                    strong_refs += 1
                
                # Check for circular references
                referrers = gc.get_referrers(obj)
                if len(referrers) > 0:
                    for referrer in referrers:
                        if hasattr(referrer, '__dict__') and obj in referrer.__dict__.values():
                            circular_refs += 1
                            break
            
            # Find unreachable objects
            unreachable = gc.collect()
            
            return {
                'strong_refs': strong_refs,
                'circular_refs': circular_refs,
                'unreachable': unreachable,
                'top_referenced_types': ref_counts.most_common(10)
            }
        except Exception as e:
            return {'error': f'Reference analysis failed: {e}'}
    
    def _analyze_ps_vm(self, context: ps.Context) -> dict[str, Any]:
        """Analyze PostScript VM memory usage."""
        if not context:
            return {}
            
        def get_dict_size(d: Any) -> int:
            """Estimate size of a PostScript dictionary."""
            if not hasattr(d, 'val') or not d.val:
                return 0
            return len(d.val)
        
        def get_vm_memory_estimate(vm_dict: Any) -> dict[str, int]:
            """Estimate memory usage of VM dictionary."""
            if not vm_dict or not hasattr(vm_dict, 'val'):
                return {'objects': 0, 'estimated_bytes': 0}
                
            obj_count = 0
            estimated_bytes = 0
            
            for key, value in vm_dict.val.items():
                obj_count += 1
                # Rough estimation - this could be more sophisticated
                estimated_bytes += sys.getsizeof(key)
                if hasattr(value, 'val'):
                    estimated_bytes += sys.getsizeof(value.val)
                else:
                    estimated_bytes += sys.getsizeof(value)
            
            return {'objects': obj_count, 'estimated_bytes': estimated_bytes}
        
        return {
            'save_id': context.save_id,
            'active_saves': len(context.active_saves) if hasattr(context, 'active_saves') else 0,
            'local_vm': get_vm_memory_estimate(context.lvm),
            'global_vm': get_vm_memory_estimate(ps.global_resources.get_gvm()),
            'string_pools': {
                'local_strings': len(context.local_strings) if context.local_strings else 0,
                'global_strings': len(ps.global_resources.global_strings) if ps.global_resources.global_strings else 0
            },
            'stacks': {
                'operand_stack': len(context.o_stack) if context.o_stack else 0,
                'execution_stack': len(context.e_stack) if context.e_stack else 0,
                'dictionary_stack': len(context.d_stack) if context.d_stack else 0,
                'graphics_stack': len(context.gstate_stack) if hasattr(context, 'gstate_stack') else 0
            }
        }
    
    def _get_tracemalloc_stats(self) -> dict[str, Any]:
        """Get tracemalloc memory tracing statistics."""
        if not self.enable_tracemalloc:
            return {}
            
        try:
            current, peak = tracemalloc.get_traced_memory()
            return {
                'current_mb': current / 1024 / 1024,
                'peak_mb': peak / 1024 / 1024
            }
        except:
            return {}
    
    def force_gc_and_measure(self, label: str, context: ps.Context | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Force garbage collection and measure before/after.
        
        Returns:
            Tuple of (before_snapshot, after_snapshot)
        """
        # Take snapshot before GC
        before = self.take_snapshot(f"{label}_before_gc", context)
        
        # Force full garbage collection
        collected_counts = []
        for generation in range(3):  # Python has 3 GC generations
            collected = gc.collect(generation)
            collected_counts.append(collected)
        
        # Take snapshot after GC
        after = self.take_snapshot(f"{label}_after_gc", context)
        
        # Calculate what was freed
        rss_freed = before['process_memory']['rss_mb'] - after['process_memory']['rss_mb']
        objects_freed = before['python_objects']['total_objects'] - after['python_objects']['total_objects']
        
        gc_result = {
            'collections_performed': collected_counts,
            'total_objects_collected': sum(collected_counts),
            'memory_freed_mb': rss_freed,
            'objects_freed': objects_freed
        }
        
        self.gc_stats.append({
            'timestamp': time.perf_counter() - self.start_time,
            'label': label,
            'before': before,
            'after': after,
            'gc_result': gc_result
        })
        
        return before, after
    
    def analyze_memory_trends(self) -> dict[str, Any]:
        """Analyze memory usage trends across snapshots."""
        if len(self.snapshots) < 2:
            return {'error': 'Need at least 2 snapshots for trend analysis'}
        
        first = self.snapshots[0]
        last = self.snapshots[-1]
        
        rss_trend = last['process_memory']['rss_mb'] - first['process_memory']['rss_mb']
        object_trend = last['python_objects']['total_objects'] - first['python_objects']['total_objects']
        
        return {
            'time_span_seconds': last['timestamp'] - first['timestamp'],
            'memory_growth_mb': rss_trend,
            'object_growth': object_trend,
            'snapshots_taken': len(self.snapshots),
            'gc_collections_total': last['garbage_collection']['total_collections'] - first['garbage_collection']['total_collections'] if 'garbage_collection' in first else 0
        }
    
    def generate_report(self) -> str:
        """Generate a comprehensive memory usage report."""
        if not self.snapshots:
            return "No memory snapshots available"
        
        report = []
        report.append("=" * 80)
        report.append("POSTFORGE MEMORY ANALYSIS REPORT")
        report.append("=" * 80)
        
        # Summary
        first = self.snapshots[0]
        last = self.snapshots[-1]
        trends = self.analyze_memory_trends()
        
        report.append(f"\nSUMMARY:")
        report.append(f"  Time Span: {trends['time_span_seconds']:.2f} seconds")
        report.append(f"  Snapshots: {len(self.snapshots)}")
        report.append(f"  Memory Growth: {trends['memory_growth_mb']:.2f} MB")
        report.append(f"  Object Growth: {trends['object_growth']:,} objects")
        
        # Current status
        report.append(f"\nCURRENT STATUS:")
        report.append(f"  RSS Memory: {last['process_memory']['rss_mb']:.2f} MB")
        report.append(f"  VMS Memory: {last['process_memory']['vms_mb']:.2f} MB")
        report.append(f"  Memory %: {last['process_memory']['percent']:.1f}%")
        report.append(f"  Total Objects: {last['python_objects']['total_objects']:,}")
        
        # PostScript VM status (if available)
        if 'postscript_vm' in last and last['postscript_vm']:
            ps_vm = last['postscript_vm']
            report.append(f"\nPOSTSCRIPT VM STATUS:")
            report.append(f"  Save ID: {ps_vm.get('save_id', 'N/A')}")
            report.append(f"  Active Saves: {ps_vm.get('active_saves', 'N/A')}")
            if 'local_vm' in ps_vm:
                report.append(f"  Local VM Objects: {ps_vm['local_vm']['objects']}")
                report.append(f"  Local VM Est. Bytes: {ps_vm['local_vm']['estimated_bytes']:,}")
            if 'global_vm' in ps_vm:
                report.append(f"  Global VM Objects: {ps_vm['global_vm']['objects']}")
                report.append(f"  Global VM Est. Bytes: {ps_vm['global_vm']['estimated_bytes']:,}")
        
        # Garbage collection stats
        if self.gc_stats:
            report.append(f"\nGARBAGE COLLECTION ACTIVITY:")
            report.append(f"  Forced Collections: {len(self.gc_stats)}")
            total_collected = sum(sum(stat['gc_result']['collections_performed']) for stat in self.gc_stats)
            report.append(f"  Total Objects Collected: {total_collected:,}")
        
        # Reference analysis
        if self.snapshots and 'reference_analysis' in last['python_objects']:
            ref_analysis = last['python_objects']['reference_analysis']
            report.append(f"\nREFERENCE ANALYSIS:")
            report.append(f"  Strong References: {ref_analysis.get('strong_refs', 'N/A')}")
            report.append(f"  Circular References: {ref_analysis.get('circular_refs', 'N/A')}")
            report.append(f"  Unreachable Objects: {ref_analysis.get('unreachable', 'N/A')}")
            
            if 'top_referenced_types' in ref_analysis:
                report.append(f"  Top Referenced Types:")
                for obj_type, count in ref_analysis['top_referenced_types'][:5]:
                    report.append(f"    {obj_type}: {count} refs")
        
        # Memory snapshots
        report.append(f"\nMEMORY SNAPSHOTS:")
        for snapshot in self.snapshots:
            report.append(f"  {snapshot['timestamp']:6.2f}s - {snapshot['label']:20} - {snapshot['process_memory']['rss_mb']:6.2f} MB - {snapshot['python_objects']['total_objects']:,} objects")
        
        report.append("=" * 80)
        
        return "\n".join(report)
    
    def analyze_leaks(self) -> str:
        """Generate detailed leak analysis report."""
        report = []
        report.append("=" * 80)
        report.append("DETAILED MEMORY LEAK ANALYSIS")
        report.append("=" * 80)
        
        # PostScript object lifecycle analysis
        if self.ps_object_lifecycle:
            report.append(f"\nPOSTSCRIPT OBJECT LIFECYCLE:")
            
            # Group by type
            by_type = defaultdict(list)
            for obj_id, data in self.ps_object_lifecycle.items():
                by_type[data['type']].append(data)
            
            for obj_type, objects in by_type.items():
                report.append(f"\n  {obj_type} ({len(objects)} instances):")
                
                # Sort by creation time
                objects.sort(key=lambda x: x['created'])
                
                high_ref_objects = [obj for obj in objects if obj['ref_count'] > 5]
                if high_ref_objects:
                    report.append(f"    High reference count objects: {len(high_ref_objects)}")
                    for obj in high_ref_objects[:3]:  # Show top 3
                        report.append(f"      Created: {obj['created']:.2f}s, Refs: {obj['ref_count']}, Referrers: {obj['referrers']}")
        
        # Reference chain analysis for PostScript objects
        report.append(f"\nREFERENCE CHAIN ANALYSIS:")
        ps_objects = [obj for obj in gc.get_objects() 
                     if hasattr(obj, '__class__') and obj.__class__.__module__ == 'ps_types']
        
        if ps_objects:
            # Analyze a sample
            sample_objects = ps_objects[:5]  # Analyze first 5 PS objects
            for obj in sample_objects:
                obj_type = obj.__class__.__name__
                referrers = gc.get_referrers(obj)
                
                report.append(f"\n  {obj_type} (id: {id(obj)}):")
                report.append(f"    Reference count: {sys.getrefcount(obj)}")
                report.append(f"    Referrers: {len(referrers)}")
                
                # Analyze referrers
                referrer_types = Counter()
                for referrer in referrers:
                    referrer_type = type(referrer).__name__
                    referrer_types[referrer_type] += 1
                
                report.append(f"    Referrer types: {dict(referrer_types)}")
        
        # Memory growth analysis
        if len(self.snapshots) >= 2:
            report.append(f"\nMEMORY GROWTH ANALYSIS:")
            
            for i in range(1, len(self.snapshots)):
                prev = self.snapshots[i-1]
                curr = self.snapshots[i]
                
                memory_delta = curr['process_memory']['rss_mb'] - prev['process_memory']['rss_mb']
                object_delta = curr['python_objects']['total_objects'] - prev['python_objects']['total_objects']
                
                if memory_delta > 0.1 or object_delta > 10:  # Significant changes
                    report.append(f"\n  {prev['label']} → {curr['label']}:")
                    report.append(f"    Memory: +{memory_delta:.2f} MB")
                    report.append(f"    Objects: +{object_delta}")
                    
                    # Compare object types
                    if 'by_type' in prev['python_objects'] and 'by_type' in curr['python_objects']:
                        prev_types = prev['python_objects']['by_type']
                        curr_types = curr['python_objects']['by_type']
                        
                        for obj_type in curr_types:
                            if obj_type in prev_types:
                                type_delta = curr_types[obj_type] - prev_types[obj_type]
                                if type_delta > 0:
                                    report.append(f"      +{type_delta} {obj_type}")
        
        report.append("=" * 80)
        return "\n".join(report)
    
    def get_reference_chains(self, obj_type: str | None = None) -> dict[str, Any]:
        """Get detailed reference chains for objects."""
        chains = defaultdict(list)
        
        objects_to_analyze = gc.get_objects()
        if obj_type:
            objects_to_analyze = [obj for obj in objects_to_analyze 
                                if type(obj).__name__ == obj_type]
        
        for obj in objects_to_analyze[:10]:  # Limit to 10 objects
            obj_id = id(obj)
            obj_type_name = type(obj).__name__
            
            # Get reference chain
            referrers = gc.get_referrers(obj)
            chain = []
            
            for referrer in referrers[:3]:  # Limit referrers analyzed
                referrer_type = type(referrer).__name__
                referrer_id = id(referrer)
                
                chain.append({
                    'type': referrer_type,
                    'id': referrer_id,
                    'size': sys.getsizeof(referrer)
                })
            
            chains[obj_type_name].append({
                'object_id': obj_id,
                'reference_chain': chain,
                'ref_count': sys.getrefcount(obj)
            })
        
        return dict(chains)


# Global profiler instance (can be enabled/disabled)
_memory_profiler: MemoryProfiler | None = None

def enable_memory_profiling(enable_tracemalloc: bool = True) -> MemoryProfiler:
    """Enable global memory profiling."""
    global _memory_profiler
    _memory_profiler = MemoryProfiler(enable_tracemalloc)
    return _memory_profiler

def get_memory_profiler() -> MemoryProfiler | None:
    """Get the global memory profiler instance."""
    return _memory_profiler

def take_memory_snapshot(label: str, context: ps.Context | None = None) -> dict[str, Any] | None:
    """Take a memory snapshot if profiling is enabled."""
    if _memory_profiler:
        return _memory_profiler.take_snapshot(label, context)
    return None

def force_gc_and_measure(label: str, context: ps.Context | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Force GC and measure if profiling is enabled."""
    if _memory_profiler:
        return _memory_profiler.force_gc_and_measure(label, context)
    return None

def generate_memory_report() -> str:
    """Generate memory report if profiling is enabled."""
    if _memory_profiler:
        return _memory_profiler.generate_report()
    return "Memory profiling not enabled"

def analyze_memory_leaks() -> str:
    """Generate detailed memory leak analysis."""
    if _memory_profiler:
        return _memory_profiler.analyze_leaks()
    return "Memory profiling not enabled"

def get_reference_chains(obj_type: str | None = None) -> dict[str, Any]:
    """Get reference chains for specific object types."""
    if _memory_profiler:
        return _memory_profiler.get_reference_chains(obj_type)
    return {}