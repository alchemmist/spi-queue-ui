#!/usr/bin/env python3
"""Download text evidence from App comments; uses gh auth and verifies SHA-256."""
import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

def reconstruct(comments,event_key):
    grouped={}
    for comment in comments:
        actor=comment.get('user',{});app=comment.get('performed_via_github_app')or{}
        if actor.get('type')!='Bot' or (app.get('id')!=5569869 if app.get('id')is not None else actor.get('id')!=351189559):continue
        body=comment.get('body')or''
        if '<!-- socrat-spi-artifact:'+event_key+':' not in body:continue
        header=re.match(r'## Evidence ([A-Z0-9]+) — ([A-Za-z0-9_.-]+) \(part ([0-9]+)/([0-9]+)\)\n\nSHA-256: `([a-f0-9]{64})`',body)
        block=re.search(r'\n\n(?P<fence>`{4,})text\n(?P<data>.*?)\n(?P=fence)\n',body,re.S)
        if not header or not block:raise ValueError('INVALID_ARTIFACT_COMMENT')
        aid,name,part,total,checksum=header.groups();key=(aid,name,checksum,int(total));number=int(part)
        if not 1<=number<=int(total)<=100 or name in ('.','..'):raise ValueError('INVALID_PART')
        pieces=grouped.setdefault(key,{})
        if number in pieces and pieces[number]!=block['data']:raise ValueError('CONFLICTING_ARTIFACT_PART')
        pieces[number]=block['data']
    output={}
    for (aid,name,checksum,total),pieces in grouped.items():
        if set(pieces)!=set(range(1,total+1)):raise ValueError('MISSING_ARTIFACT_PART')
        data=''.join(pieces[n]for n in range(1,total+1)).encode()
        if hashlib.sha256(data).hexdigest()!=checksum:raise ValueError('ARTIFACT_CHECKSUM_MISMATCH')
        output[aid+'-'+name]=data
    if not output:raise ValueError('NO_EVIDENCE_FOR_EVENT')
    manifest=next((json.loads(v)for k,v in output.items()if k.startswith('MANIFEST-')),None)
    if not manifest or manifest.get('event_key')!=event_key:raise ValueError('MISSING_OR_WRONG_MANIFEST')
    for artifact in manifest['artifacts']:
        key=artifact['id']+'-'+artifact['name']
        if artifact['stored_chars'] and key not in output:raise ValueError('MISSING_MANIFEST_ARTIFACT')
        if key in output and hashlib.sha256(output[key]).hexdigest()!=artifact['sha256']:raise ValueError('MANIFEST_CHECKSUM_MISMATCH')
    return output

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--issue',type=int,required=True);p.add_argument('--event',required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if args.issue<=0 or not re.fullmatch('[a-f0-9]{64}',args.event):p.error('positive issue and 64-character event key required')
    pages=json.loads(subprocess.check_output(['gh','api','repos/alchemmist/homelab/issues/'+str(args.issue)+'/comments','--paginate','--slurp']))
    files=reconstruct([c for page in pages for c in page],args.event)
    os.umask(0o077);args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name,data in files.items():
        path=args.output/name
        if path.exists():
            if path.is_symlink()or path.read_bytes()!=data:raise ValueError('EXISTING_OUTPUT_DIFFERS')
            continue
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb')as stream:stream.write(data)
    print('Verified and saved '+str(len(files))+' evidence artifacts.')
if __name__=='__main__':main()
