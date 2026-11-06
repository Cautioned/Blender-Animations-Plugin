"""Local-only diagnostic assets and bounded live-property snapshots."""
import base64,json,sys,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'visual_parity'))
from bootstrap import load_addon_modules
load_addon_modules()
from roblox_animations.rig.pngdec import decode_png
root=Path('build/light_parity').resolve()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path!='/assets':self.send_error(404);return
        maps={}
        for path in (root/'assets').glob('*.png'):
            w,h,_,pixels=decode_png(path.read_bytes())
            # pngdec returns float buffer without numpy in system Python.
            try:raw=pixels.tobytes()
            except AttributeError:raw=bytes(round(v*255) for v in pixels)
            maps[path.stem]={'width':w,'height':h,'rgba':base64.b64encode(raw).decode()}
        data=json.dumps(maps).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
    def do_POST(self):
        if self.path not in ('/snapshot','/uploads','/model'):self.send_error(404);return
        size=int(self.headers.get('Content-Length',0))
        if not 0<size<4*1024*1024:self.send_error(400);return
        data=json.loads(self.rfile.read(size));name=data.get('id','uploads')
        if not name.replace('_','').isalnum():self.send_error(400);return
        folder=root/'snapshots';folder.mkdir(exist_ok=True)
        if self.path=='/model':
            folder=root/'roundtrip';folder.mkdir(exist_ok=True)
            (folder/(name+'.rbxm')).write_bytes(base64.b64decode(data.pop('rbxm'),validate=True))
        (folder/(name+'.json')).write_text(json.dumps(data,indent=2))
        self.send_response(200);self.end_headers();self.wfile.write(b'{"saved":true}')
    def log_message(self,*args):pass
print('Material fixture data server on localhost:18766',flush=True)
HTTPServer(('127.0.0.1',18766),Handler).serve_forever()
