"""Receive fixture-only RGBA captures from Studio on localhost; stdlib only."""
import argparse
import base64
import hashlib
import json
import re
import struct
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def png_bytes(width, height, rgba):
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    rows = b''.join(b'\0' + rgba[y*width*4:(y+1)*width*4] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',width,height,8,6,0,0,0))
            + chunk(b'IDAT',zlib.compress(rows)) + chunk(b'IEND',b''))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('build/visual_parity/studio'))
    parser.add_argument('--port',type=int,default=18765)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)

    pending = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length=int(self.headers.get('Content-Length',0))
                if self.path!='/capture' or not 0<length<=64*1024*1024:
                    self.send_error(400);return
                data=json.loads(self.rfile.read(length))
                name=data['case'];width=int(data['width']);height=int(data['height'])
                if not re.fullmatch(r'[0-9]{2}_[a-z0-9_]+',name) or not 0<width<=4096 or not 0<height<=4096:
                    raise ValueError('Invalid fixture capture')
                raw=base64.b64decode(data.pop('rgba_base64'),validate=True)
                y=int(data.pop('chunk_y',0))
                rows=int(data.pop('chunk_height',height))
                if not 0<=y<height or not 0<rows<=height-y or len(raw)!=width*rows*4:
                    raise ValueError('RGBA stripe size mismatch')
                if y==0:
                    pending[name]={'width':width,'height':height,'raw':bytearray(width*height*4),'rows':set()}
                capture=pending[name]
                if capture['width']!=width or capture['height']!=height:
                    raise ValueError('Capture dimensions changed')
                capture['raw'][y*width*4:(y+rows)*width*4]=raw
                capture['rows'].update(range(y,y+rows))
                if len(capture['rows'])<height:
                    reply=json.dumps({'case':name,'rows_received':len(capture['rows'])}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(reply)))
                    self.end_headers();self.wfile.write(reply)
                    return
                raw=bytes(capture['raw'])
                del pending[name]
                path=args.output/(name+'.png')
                path.write_bytes(png_bytes(width,height,raw))
                data['sha256_rgba']=hashlib.sha256(raw).hexdigest()
                (args.output/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
                reply=json.dumps({'saved':name,'width':width,'height':height,'sha256_rgba':data['sha256_rgba']}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(reply)));self.end_headers();self.wfile.write(reply)
                print('CAPTURE_SAVED '+name,flush=True)
            except Exception as exc:
                self.send_error(400,str(exc))

        def log_message(self,fmt,*args):
            pass

    print(f'Capture receiver: http://127.0.0.1:{args.port}/capture',flush=True)
    HTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__=='__main__':main()
