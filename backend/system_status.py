"""Read-only local resource information for the workspace dashboard."""
from __future__ import annotations
import ctypes
import os
import shutil
import subprocess
import threading
import time

_gpu_cache={'at':0,'value':None}
_gpu_lock=threading.Lock()


def memory_status():
    if os.name!='nt':return None
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),*[(name,ctypes.c_ulonglong) for name in ('total','available','total_page','available_page','total_virtual','available_virtual','extended')]]
    status=Memory();status.length=ctypes.sizeof(status)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {'total_bytes':status.total,'available_bytes':status.available,'used_percent':status.load}
    return None


def gpu_status():
    with _gpu_lock:
        if time.monotonic()-_gpu_cache['at']<30:return _gpu_cache['value']
        value=None
        try:
            result=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,utilization.gpu','--format=csv,noheader,nounits'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=2,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if result.returncode==0:
                name,total,used,utilization=[v.strip() for v in result.stdout.splitlines()[0].split(',')]
                value={'name':name,'total_bytes':int(total)*1024*1024,'used_bytes':int(used)*1024*1024,'utilization_percent':int(utilization)}
        except (OSError,ValueError,IndexError,subprocess.TimeoutExpired):pass
        _gpu_cache.update(at=time.monotonic(),value=value)
        return value


def system_status(data_dir,models_dir):
    volume=models_dir
    while not volume.exists() and volume.parent!=volume:volume=volume.parent
    disk=shutil.disk_usage(volume)
    return {'memory':memory_status(),'gpu':gpu_status(),'disk':{'total_bytes':disk.total,'free_bytes':disk.free,'used_bytes':disk.used},
            'data_dir':str(data_dir),'models_dir':str(models_dir),'cpu_threads':os.cpu_count()}
