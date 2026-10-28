"""Build one or both self-contained skill ZIPs using only the standard library."""
import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git','__pycache__','.venv','venv','.pytest_cache','.tmp','.work','outputs'}


def package(name):
    manifest=json.loads((ROOT/'versions.json').read_text(encoding='utf-8'))
    info=manifest['skills'][name]
    source=(ROOT/info['path']).resolve()
    if source.parent != ROOT or source.name != name:
        raise ValueError('Unexpected skill directory')
    version=info['version'].removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?',version):
        raise ValueError('Invalid version')
    if (source/'VERSION').exists():
        recorded=(source/'VERSION').read_text(encoding='utf-8-sig').strip().removeprefix('v')
    else:
        front=(source/'SKILL.md').read_text(encoding='utf-8-sig').split('---',2)[1]
        match=re.search(r'^\s+version:\s*[\"\x27]?([\w.+-]+)',front,re.M)
        if not match: raise ValueError('Skill version metadata missing')
        recorded=match[1].removeprefix('v')
    if recorded != version: raise ValueError(name+': update versions.json and the skill version consistently')
    files=[]
    for file in source.rglob('*'):
        relative=file.relative_to(source)
        if EXCLUDED.intersection(relative.parts) or file.is_symlink() or not file.is_file(): continue
        if file.name.endswith(('.pyc','.pyo','.journal.json','.journal.json.lock')): continue
        if file.name=='.env' or (file.name.startswith('.env.') and file.name!='.env.example'): continue
        files.append(file)
    if not (source/'SKILL.md').is_file(): raise ValueError('Incomplete skill')
    destination=ROOT/'packages'/f'{name}-v{version}.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            entry=zipfile.ZipInfo(file.relative_to(ROOT).as_posix(),date_time=(1980,1,1,0,0,0))
            entry.external_attr=0o100644 << 16
            archive.writestr(entry,file.read_bytes(),compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip(): raise ValueError('Archive CRC check failed')
    return {'skill':name,'version':version,'files':len(files),'archive':str(destination.relative_to(ROOT))}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill',choices=('zotero-input','zotero-output'))
    args=parser.parse_args()
    for name in ([args.skill] if args.skill else ['zotero-input','zotero-output']):
        print(json.dumps(package(name),ensure_ascii=False))
