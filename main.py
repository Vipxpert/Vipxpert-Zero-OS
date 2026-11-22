# /vipxpert_minimal_os/main.py

import asyncio
import signal
import sys
import os
import time
import threading
import traceback
from src.core.os_kernel import OSKernel

# --- AUTOPSY SUBSYSTEM GLOBALS ---
_last_loop_tick = time.monotonic()
_is_shutting_down = False

async def _event_loop_heartbeat():
    """Keeps the Autopsy Canary informed that the asyncio loop is alive."""
    global _last_loop_tick, _is_shutting_down
    try:
        while not _is_shutting_down:
            _last_loop_tick = time.monotonic()
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        _is_shutting_down = True

def _autopsy_canary_thread():
    """Flight Recorder: Photographs stack traces if the event loop freezes for > 5 seconds."""
    while not _is_shutting_down:
        time.sleep(2)
        if _is_shutting_down:
            break
        gap = time.monotonic() - _last_loop_tick
        
        if gap > 5.0:
            try:
                with open("vampire_crash.txt", "w", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"🚨 [AUTOPSY] EVENT LOOP STARVATION DETECTED at {time.ctime()}\n")
                    f.write(f"Loop unresponsive for {gap:.1f} seconds! Dumping stack traces...\n\n")
                    
                    threads = {th.ident: th.name for th in threading.enumerate()}
                    for thread_id, frame in sys._current_frames().items():
                        t_name = threads.get(thread_id, f"UnknownThread-{thread_id}")
                        f.write(f"--- Thread: {t_name} ---\n")
                        f.write("".join(traceback.format_stack(frame)))
                        f.write("\n")
                    f.write(f"{'='*60}\n")
            except Exception:
                pass
            
            # Sleep heavily so we don't spam the disk while frozen
            time.sleep(20)

def _autopsy_memory_tracer_thread():
    """X-Ray Monitor: Tracks uncollected memory garbage and leaks over time."""
    import tracemalloc
    
    # Start tracing memory allocations globally, capturing 25 frames of history!
    tracemalloc.start(25)
    
    # Wait 30 seconds for the OS to fully boot and cache initial UI data
    time.sleep(30)
    baseline_snapshot = tracemalloc.take_snapshot()
    
    while not _is_shutting_down:
        # Check for leaks every 60 seconds
        time.sleep(60)
        if _is_shutting_down:
            break
        try:
            current_snapshot = tracemalloc.take_snapshot()
            top_stats = current_snapshot.compare_to(baseline_snapshot, 'lineno')
            
            # Filter for significant growth (> 256KB leaked by a single line of code)
            significant_leaks = [stat for stat in top_stats if stat.size_diff > 256 * 1024]
                        
            if significant_leaks:
                with open("vampire_memory.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"🧠 [AUTOPSY] MEMORY LEAK DETECTED at {time.ctime()}\n")
                    f.write("Significant uncollected garbage identified since boot:\n\n")
                    
                    for stat in significant_leaks[:15]:  # Top 15 worst offenders
                        if stat.traceback:
                            # 1. Walk backward through the history to find YOUR code
                            app_frame = None
                            for frame in stat.traceback:
                                norm = frame.filename.replace('\\', '/')
                                if 'vipxpert_minimal_os' in norm and 'site-packages' not in norm and 'Python' not in norm:
                                    app_frame = frame
                                    break
                                    
                            # 2. Extract the physical allocation site (the library)
                            lib_frame = stat.traceback[0]
                            lib_path = lib_frame.filename.replace('\\', '/').split('/')[-1]
                            size_kb = stat.size_diff / 1024
                            
                            if app_frame:
                                app_file = app_frame.filename.replace('\\', '/').split('vipxpert_minimal_os/')[-1]
                                f.write(f"🔴 {app_file}:{app_frame.lineno}\n")
                                f.write(f"   └─ Allocated {size_kb:.1f} KB via {lib_path}:{lib_frame.lineno} ({stat.count_diff} objects)\n")
                            else:
                                f.write(f"🟡 [Internal/Lib] {lib_path}:{lib_frame.lineno} -> +{size_kb:.1f} KB ({stat.count_diff} objects)\n")
                        else:
                            f.write(str(stat) + "\n")
                        
                    f.write(f"\n{'='*60}\n")
                
                # Update the baseline so we don't spam the exact same leak indefinitely
                baseline_snapshot = current_snapshot
        except Exception:
            pass

def _autopsy_ram_canary_thread():
    """Main Process RAM Monitor: Photographs stack if OS violates a 2.5GB RAM ceiling."""
    try:
        import psutil
    except ImportError:
        print("[Autopsy] Warning: 'psutil' not installed. RAM Canary disabled.")
        return
        
    main_process = psutil.Process(os.getpid())
    threshold_bytes = 2.5 * 1024 * 1024 * 1024  # 2.5 GB
    
    while not _is_shutting_down:
        time.sleep(5)
        if _is_shutting_down:
            break
        try:
            current_ram = main_process.memory_info().rss
            if current_ram > threshold_bytes:
                
                # The GC might just be lazy. Force a massive sweep.
                import gc
                gc.collect()
                time.sleep(1) # Give OS a moment to reclaim memory
                
                # Check if the GC saved us
                current_ram = main_process.memory_info().rss
                if current_ram < threshold_bytes:
                    continue # False alarm, GC did its job. Back to polling!

                with open("vampire_crash.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"🚨 [AUTOPSY] MASSIVE RAM SPIKE DETECTED at {time.ctime()}\n")
                    f.write(f"OS is consuming {current_ram / (1024**3):.2f} GB! Dumping threads...\n\n")
                    
                    threads = {th.ident: th.name for th in threading.enumerate()}
                    idle_thread_count = 0
                    
                    # 🚦 SIGNAL-TO-NOISE: Filter out idling library workers
                    for thread_id, frame in sys._current_frames().items():
                        t_name = threads.get(thread_id, f"UnknownThread-{thread_id}")
                        stack_lines = traceback.format_stack(frame)
                        
                        is_guilty = False
                        formatted_stack = []
                        
                        for line in stack_lines:
                            norm_line = line.replace('\\', '/')
                            if "vipxpert_minimal_os" in norm_line and "site-packages" not in norm_line and "Python" not in norm_line:
                                is_guilty = True
                                formatted_stack.append(f" 🎯 ===> {line.strip()}")
                            else:
                                formatted_stack.append(f"    {line.strip()}")
                                
                        if is_guilty:
                            f.write(f"\n--- Thread: {t_name} (ID: {thread_id}) ---\n")
                            f.write("\n".join(formatted_stack) + "\n")
                        else:
                            idle_thread_count += 1
                            
                    if idle_thread_count > 0:
                        f.write(f"\n👻 [Filtered {idle_thread_count} background/library threads that were idling or waiting on locks]\n")
                        
                    f.write(f"\n{'='*60}\n")
                
                # Sleep for 5 minutes to prevent spamming the disk while the server attempts to recover or die
                time.sleep(300)
        except Exception:
            pass

def _autopsy_cpu_sampler_thread():
    """CPU X-Ray Sampler: Samples ALL threads 50x/sec to identify exact CPU-hogging functions."""
    from collections import defaultdict
    
    # Give the OS 15 seconds to finish booting before sampling
    time.sleep(15)
    
    while not _is_shutting_down:
        thread_counts = defaultdict(lambda: defaultdict(int))
        snapshots = 0
        
        # Sample for 30 seconds (1500 samples @ 50Hz)
        for _ in range(1500):
            if _is_shutting_down:
                return
            time.sleep(0.02)
            snapshots += 1
            
            # Get names for all current threads to map IDs
            t_names = {t.ident: t.name for t in threading.enumerate()}
            
            for tid, frame in sys._current_frames().items():
                t_name = t_names.get(tid, f"Thread-{tid}")
                
                # Skip the Autopsy sampler itself so it doesn't pollute the data
                if t_name == "AutopsyCPUSampler":
                    continue
                    
                stack_trace = []
                curr = frame
                while curr and len(stack_trace) < 3:
                    code = curr.f_code
                    filename = os.path.basename(code.co_filename)
                    stack_trace.append(f"{filename}:{curr.f_lineno}[{code.co_name}]")
                    curr = curr.f_back
                
                key = " <- ".join(stack_trace)
                thread_counts[t_name][key] += 1
                
        if snapshots > 0:
            try:
                with open("vampire_cpu_top.txt", "w", encoding="utf-8") as f:
                    f.write(f"\n{'='*90}\n")
                    f.write(f"⚡ [AUTOPSY] MULTI-THREAD CPU PROFILE (30s Window at {time.ctime()})\n")
                    f.write(f"Total Snapshots: {snapshots} | Target: ALL Active Threads\n")
                    f.write(f"Percentage = Time spent in this function relative to the thread's total lifespan\n")
                    f.write(f"{'='*90}\n")
                    
                    for t_name, counts in thread_counts.items():
                        f.write(f"\n--- Thread: {t_name} ---\n")
                        f.write(f"{'PERCENT':<8} | {'EXECUTION TRACE (Leaf <- Caller <- Parent)':<70}\n")
                        f.write(f"{'-'*90}\n")
                        
                        sorted_stats = sorted(counts.items(), key=lambda x: x[1], reverse=True)
                        for loc, count in sorted_stats[:5]:
                            pct = (count / snapshots) * 100
                            f.write(f"{pct:6.2f}%  | {loc}\n")
                            
                    f.write(f"\n{'='*90}\n")
            except Exception:
                pass

async def _autopsy_event_loop_latency_monitor():
    """Traffic Camera: Detects Event Loop saturation by measuring task queue latency."""
    import collections
    probe_interval = 1.0  # Check every 1 second continuously
    
    while True:
        start_time = time.monotonic()
        
        # The sleep itself IS the probe! This eliminates all blind spots.
        await asyncio.sleep(probe_interval)
        
        latency = (time.monotonic() - start_time) - probe_interval
        
        # If the probe sat in traffic for more than 1.5 seconds, we have a severe jam
        if latency > 1.5:
            try:
                tasks = asyncio.all_tasks()
                
                # 🚦 SIGNAL-TO-NOISE: Group tasks by their actual coroutine names
                task_counts = collections.Counter()
                for t in tasks:
                    coro = t.get_coro()
                    name = getattr(coro, '__qualname__', getattr(coro, '__name__', t.get_name())) if coro else t.get_name()
                    task_counts[name] += 1
                    
                with open("vampire_latency.txt", "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"🚦 [AUTOPSY] EVENT LOOP CONGESTION DETECTED at {time.ctime()}\n")
                    f.write(f"Traffic Jam! A background task was delayed by {latency:.2f} seconds.\n")
                    f.write(f"There are currently {len(tasks)} async tasks fighting for the CPU right now:\n\n")
                    
                    for task_name, count in task_counts.most_common(15): # Cap at top 15
                        f.write(f"  👉 {count}x: {task_name}\n")
                        
                    if len(task_counts) > 15:
                        f.write(f"  ... and {len(task_counts) - 15} other unique task types.\n")
                        
                    f.write(f"{'='*60}\n")
            except Exception:
                pass

# --- OS BOOTLOADER ---

async def main():
    kernel = OSKernel()
    loop = asyncio.get_running_loop()

    # 1. Ignite the Autopsy Subsystems in the background
    # print("[Bootloader] Arming Autopsy monitors...", flush=True)
    # threading.Thread(target=_autopsy_canary_thread, daemon=True, name="AutopsyCanary").start()
    # threading.Thread(target=_autopsy_memory_tracer_thread, daemon=True, name="AutopsyXRay").start()
    # threading.Thread(target=_autopsy_ram_canary_thread, daemon=True, name="AutopsyRAM").start()
    # threading.Thread(target=_autopsy_cpu_sampler_thread, daemon=True, name="AutopsyCPUSampler").start()
    # asyncio.create_task(_event_loop_heartbeat())
    # asyncio.create_task(_autopsy_event_loop_latency_monitor())

    def shutdown_handler():
        global _is_shutting_down
        _is_shutting_down = True
        print("\n[Bootloader] Termination signal received. Initiating graceful shutdown...", flush=True)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: shutdown_handler())

    print("[Bootloader] Handing execution to OS Kernel...", flush=True)
    try:
        await kernel.run()
    except asyncio.CancelledError:
        print("[Bootloader] Kernel execution cleanly cancelled.", flush=True)
    except Exception as e:
        print(f"[Bootloader] FATAL KERNEL PANIC: {e}", flush=True)
    finally:
        global _is_shutting_down
        _is_shutting_down = True
        print("[Bootloader] OS offline. Hardware released. Goodbye.", flush=True)

# --- EXECUTION WRAPPERS ---

def _run_with_profiler():
    """Wraps the OS bootloader in a pyinstrument CPU profiler."""
    try:
        from pyinstrument import Profiler
        profiler = Profiler(async_mode='enabled')
        profiler.start()
    except ImportError:
        print("[Bootloader] pyinstrument not installed, CPU profiling disabled.")
        _run_standard()
        return

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        profiler.stop()
        print("\n[Bootloader] Writing CPU profile to vampire_cpu_profile.txt...")
        with open("vampire_cpu_profile.txt", "w", encoding="utf-8") as f:
            f.write(profiler.output_text(unicode=True, color=False, show_all=True))
        print("[Bootloader] CPU Profile saved successfully!")

def _run_standard():
    """Runs the OS normally without CPU profiling overhead."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Run the OS safely.
    _run_standard()
    
    # ⚠️ WARNING: Running the profiler 24/7 will cause a massive memory leak!
    # Uncomment the line below (and comment the line above) ONLY when debugging CPU execution time.
    # _run_with_profiler()
