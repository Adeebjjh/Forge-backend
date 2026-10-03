"""Generate the source archive and the self-contained APK workflow."""
import argparse
import base64
import hashlib
import io
from pathlib import Path
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def archive(paths, prefix=''):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(paths):
            if p.is_file() and not any(part in ('__pycache__','.git','.gradle','build','node_modules','.ui-test-deps') for part in p.relative_to(ROOT).parts):
                z.write(p, prefix + p.relative_to(ROOT).as_posix())
    return out.getvalue()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    out=parser.parse_args().output
    out.mkdir(parents=True,exist_ok=True)
    source=archive(ROOT.rglob('*'),'forge-agent/')
    (out/'Forge-Agent-Source.zip').write_bytes(source)
    template=(ROOT/'packaging/build-apk.template.yml').read_text()
    encoded='\n'.join('            '+line for line in textwrap.wrap(base64.b64encode(source).decode(),120))
    (out/'Build-Forge-APK.yml').write_text(template.replace('__SOURCE_B64__',encoded).replace('__SOURCE_SHA__',hashlib.sha256(source).hexdigest()))
    print('Created source ZIP and APK workflow in',out)

if __name__=='__main__': main()
