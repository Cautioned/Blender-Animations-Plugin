"""Optional macOS native PNG decode for large comparison batches."""
import hashlib,struct,subprocess,tempfile
from pathlib import Path
import numpy as np

def pixels(path):
 path=Path(path).resolve()
 cache=Path(tempfile.gettempdir())/'rbx-light-png';cache.mkdir(exist_ok=True)
 target=cache/(hashlib.sha256((str(path)+str(path.stat().st_mtime_ns)).encode()).hexdigest()+'.bmp')
 if not target.exists():subprocess.run(['/usr/bin/sips','-s','format','bmp',str(path),'--out',str(target)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 raw=target.read_bytes();assert raw[:2]==b'BM'
 offset=struct.unpack_from('<I',raw,10)[0];w,h=struct.unpack_from('<ii',raw,18);bits=struct.unpack_from('<H',raw,28)[0]
 assert bits in (24,32)
 channels=bits//8;stride=((w*channels+3)//4)*4
 array=np.frombuffer(raw,dtype=np.uint8,offset=offset,count=stride*abs(h)).reshape(abs(h),stride)[:,:w*channels].reshape(abs(h),w,channels)
 if h>0:array=array[::-1]
 return array[:,:,[2,1,0]].copy()
