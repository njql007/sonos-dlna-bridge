#!/usr/bin/env python3
"""Install/manage a macOS user LaunchAgent. Does not require administrator access."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = 'local.sonos.dlna-bridge'
BASE = Path.home() / 'Library/Application Support/SonosDLNABridge'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
DOMAIN = 'gui/' + str(os.getuid())

def run(*args, check=True):
    return subprocess.run(['launchctl', *args], check=check)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install','start','stop','status','uninstall'])
    parser.add_argument('--target', help='Sonos IP address or hostname (required for install)')
    parser.add_argument('--name',default='Sonos DLNA')
    args=parser.parse_args()
    if sys.platform!='darwin': parser.error('Use bridge.py directly on other platforms')
    if args.action=='install':
        if not args.target: parser.error('--target is required for install')
        BASE.mkdir(parents=True,exist_ok=True);PLIST.parent.mkdir(parents=True,exist_ok=True)
        if PLIST.exists():run('bootout',DOMAIN,str(PLIST),check=False)
        shutil.copy2(Path(__file__).with_name('bridge.py'),BASE/'bridge.py')
        data={'Label':LABEL,'ProgramArguments':[sys.executable,'-u',str(BASE/'bridge.py'),'--target',args.target,'--name',args.name,'--log',str(BASE/'bridge.log'),'--state',str(BASE/'media-state.json')],
              'WorkingDirectory':str(BASE),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
              'StandardOutPath':str(BASE/'stdout.log'),'StandardErrorPath':str(BASE/'stderr.log')}
        PLIST.write_bytes(plistlib.dumps(data));run('bootstrap',DOMAIN,str(PLIST))
        print('Installed and started:',BASE)
    elif args.action=='start':run('bootstrap',DOMAIN,str(PLIST))
    elif args.action=='stop':run('bootout',DOMAIN,str(PLIST))
    elif args.action=='status':run('print',DOMAIN+'/'+LABEL)
    elif args.action=='uninstall':
        run('bootout',DOMAIN,str(PLIST),check=False)
        if PLIST.exists():PLIST.unlink()
        print('Autostart removed. Program and logs retained at',BASE)

if __name__=='__main__':main()
