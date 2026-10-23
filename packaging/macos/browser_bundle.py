"""Build-only macOS browser tree copy/sign/check; no fetching or cache writes."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess

VERSION='1.58.0'
EXPECTED={'chromium':'1208','chromium-headless-shell':'1208','ffmpeg':'1011'}
NAMES=('chromium-1208','chromium_headless_shell-1208','ffmpeg-1011')
ENTRYPOINTS=(
    'chrome-mac-arm64/Google Chrome for Testing.app',
    'chrome-headless-shell-mac-arm64/chrome-headless-shell',
    'ffmpeg-mac',
)


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024): digest.update(chunk)
    return digest.hexdigest()


def _driver_manifest():
    if importlib.metadata.version('playwright')!=VERSION:
        raise RuntimeError('Mac browser build requires locked Playwright '+VERSION)
    return Path(importlib.metadata.distribution('playwright').locate_file('playwright/driver/package/browsers.json'))


def selected_roots(browser_root,driver_manifest):
    data=json.loads(Path(driver_manifest).read_text())
    selected={row['name']:row for row in data['browsers'] if row['name'] in EXPECTED}
    if {name:row['revision'] for name,row in selected.items()}!=EXPECTED:
        raise RuntimeError('Playwright browser revision does not match admitted Mac inputs')
    roots=[Path(browser_root)/name for name in NAMES]
    for root,entry in zip(roots,ENTRYPOINTS):
        if root.is_symlink() or not root.is_dir() or not all((root/name).is_file() for name in ('INSTALLATION_COMPLETE','DEPENDENCIES_VALIDATED')):
            raise RuntimeError('Pinned Playwright browser tree incomplete: '+root.name)
        if not (root/entry).exists(): raise RuntimeError('Pinned browser entrypoint incomplete: '+root.name)
    app=roots[0]/ENTRYPOINTS[0]
    with (app/'Contents/Info.plist').open('rb') as stream: info=plistlib.load(stream)
    if info['CFBundleShortVersionString']!=selected['chromium']['browserVersion']:
        raise RuntimeError('Chromium bundle version does not match driver')
    return roots


def fingerprint(root):
    root=Path(root); result=[]
    for path in [root,*sorted(root.rglob('*'))]:
        info=path.lstat(); row={'path':str(path.relative_to(root)),'mode':stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            target=os.readlink(path)
            if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or not path.exists():
                raise RuntimeError('Browser tree symlink escapes or is missing')
            row.update(kind='symlink',target=target)
        elif stat.S_ISREG(info.st_mode): row.update(kind='file',size_bytes=info.st_size,sha256=_sha(path))
        elif stat.S_ISDIR(info.st_mode): row.update(kind='directory')
        else: raise RuntimeError('Unsupported browser tree entry')
        result.append(row)
    return result


def _digest(entries):
    return hashlib.sha256(json.dumps(entries,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _signature(path):
    result=subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(path)],capture_output=True,text=True)
    return {'valid':result.returncode==0,'returncode':result.returncode}


def _ditto(source,target):
    subprocess.run(['/usr/bin/ditto',str(source),str(target)],check=True)


def _sign(root):
    """Sign complete copied containers inside-out; never mutate the source cache."""
    root=Path(root)
    bundles=[path for path in root.rglob('*') if path.is_dir() and not path.is_symlink() and path.suffix in ('.app','.framework')]
    mains=set()
    for bundle in bundles:
        plist=bundle/('Contents/Info.plist' if bundle.suffix=='.app' else 'Resources/Info.plist')
        with plist.open('rb') as stream: name=plistlib.load(stream)['CFBundleExecutable']
        mains.add((bundle/('Contents/MacOS' if bundle.suffix=='.app' else '')/name).resolve())
    targets=[]
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink(): continue
        with path.open('rb') as stream: magic=stream.read(4)
        if magic in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca') and path.resolve() not in mains:
            targets.append(path)
    targets+=sorted(bundles,key=lambda path:len(path.parts),reverse=True)
    for path in targets:
        subprocess.run(['/usr/bin/codesign','--force','--sign','-','--timestamp=none',str(path)],check=True,capture_output=True)


def stage(browser_root,app_root,*,driver_manifest=None):
    driver_manifest=Path(driver_manifest) if driver_manifest is not None else _driver_manifest()
    sources=selected_roots(browser_root,driver_manifest)
    app=Path(app_root); destination=app/'Contents/Resources/playwright-browsers'
    if destination.exists(): raise RuntimeError('Browser destination must be new; no merging of revisions')
    destination.mkdir(parents=True); records=[]
    for source,entry in zip(sources,ENTRYPOINTS):
        before=fingerprint(source); signature=_signature(source/entry)
        target=destination/source.name; _ditto(source,target); copied=fingerprint(target)
        if copied!=before: raise RuntimeError('Complete browser clone identity mismatch before signing')
        _sign(target)
        final_signature=_signature(target/entry)
        if not final_signature['valid']: raise RuntimeError('Copied browser strict signature verification failed')
        final=fingerprint(target)
        if fingerprint(source)!=before: raise RuntimeError('Source browser identity changed during copy/sign')
        records.append({'name':source.name,'source_entries':before,'source_tree_sha256':_digest(before),
            'source_signature':signature,'clone_pre_sign_tree_sha256':_digest(copied),
            'final_entries':final,'final_tree_sha256':_digest(final),'clone_signature':final_signature,
            'signing':'development copy only; bottom-up ad-hoc'})
    framework_root=app/'Contents/Frameworks';framework_root.mkdir(parents=True,exist_ok=True)
    (framework_root/'playwright-browsers').symlink_to('../Resources/playwright-browsers')
    inventory={'schema_version':1,'playwright_version':VERSION,'driver_manifest_sha256':_sha(driver_manifest),
        'revisions':EXPECTED,'trees':records,'browser_runtime':'not tested; run bundled-node about:blank smoke'}
    (app/'Contents/Resources/browser-inventory.json').write_text(json.dumps(inventory,indent=2)+'\n')
    (framework_root/'browser-inventory.json').symlink_to('../Resources/browser-inventory.json')
    return inventory


def check(app_root):
    app=Path(app_root); resource=app/'Contents/Frameworks'
    inventory=json.loads((resource/'browser-inventory.json').read_text())
    if inventory['playwright_version']!=VERSION or inventory['revisions']!=EXPECTED:
        raise RuntimeError('Bundled browser driver policy mismatch')
    roots=resource/'playwright-browsers'
    if {path.name for path in roots.iterdir()}!=set(NAMES): raise RuntimeError('Unexpected bundled browser revision')
    if [row['name'] for row in inventory['trees']]!=list(NAMES): raise RuntimeError('Browser inventory incomplete')
    for row,entry in zip(inventory['trees'],ENTRYPOINTS):
        if _digest(row['source_entries'])!=row['source_tree_sha256'] or row['clone_pre_sign_tree_sha256']!=row['source_tree_sha256']:
            raise RuntimeError('Browser pre-sign identity mismatch')
        actual=fingerprint(roots/row['name'])
        if actual!=row['final_entries'] or _digest(actual)!=row['final_tree_sha256']:
            raise RuntimeError('Bundled browser tree identity mismatch')
        if not _signature(roots/row['name']/entry)['valid']: raise RuntimeError('Bundled browser signature failed')
    return inventory


SMOKE_JS=r"""
const fs=require('fs');
const path=require('path');
// Persist the private owner before any browser import/spawn. This is smoke
// supervision only; the installed driver and normal product remain unchanged.
fs.writeFileSync(path.join(process.argv[2],'owner.json'),JSON.stringify({pid:process.pid}),{flag:'wx',mode:0o600});
process.env.PLAYWRIGHT_BROWSERS_PATH=path.resolve(process.argv[3]);
const admitted=new Set([
  'chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
  'chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell'
].map(relative=>fs.realpathSync(path.join(process.env.PLAYWRIGHT_BROWSERS_PATH,relative))));
const children=require('child_process'),spawn=children.spawn;
children.spawn=(command,args,options)=>{
  if(!admitted.has(fs.realpathSync(command)))throw Error('Unexpected smoke child executable');
  return spawn(command,args,{...options,detached:false});
};
const {chromium}=require(process.argv[1]);
(async()=>{
  const results=[];
  for(const [name,channel] of [['chromium-new-headless','chromium'],['chromium-headless-shell',undefined]]){
    let context;
    try{
      context=await chromium.launchPersistentContext(process.argv[2]+'/'+name,{
        headless:true,channel,timeout:20000,args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run']});
      await context.route(/^https?:\/\//,route=>route.abort());
      const page=context.pages()[0]||await context.newPage();
      await page.goto('about:blank');
      if(page.url()!=='about:blank')throw Error('Unexpected browser navigation');
      results.push({kind:name,url:page.url(),version:context.browser().version()});
    }finally{if(context)await context.close();}
  }
  process.stdout.write(JSON.stringify({bundled_node:true,website_navigation:false,closed:true,browsers:results}));
})().catch(()=>{process.stderr.write('Bundled browser about:blank check failed');process.exitCode=1;});
"""


def _run_owned_smoke(tool,arguments,profile,*,timeout=50):
    """Reuse reviewed supervision, then verify its owned group before removal."""
    import shutil
    import time
    try:
        from yijianjifa.features.downloads.media import _run_process
        from yijianjifa.features.downloads.contracts import DownloadError
    except ImportError:
        raise RuntimeError('Browser smoke requires project source: PYTHONPATH=src') from None
    class NotCancelled:
        def cancelled(self): return False
    try:
        return _run_process(tool,tuple(arguments),cancel=NotCancelled(),deadline=time.monotonic()+timeout,
                            grace=.15,output_limit=16384)
    except DownloadError as error:
        raise RuntimeError('Bundled browser process failed: '+error.code) from None
    finally:
        owner=Path(profile)/'owner.json'
        try:
            if owner.is_symlink() or owner.stat().st_size>64: raise ValueError
            data=json.loads(owner.read_text())
            if set(data)!={'pid'} or type(data['pid']) is not int or not 1<data['pid']<2**31: raise ValueError
            pid=data['pid']
        except (OSError,ValueError,TypeError):
            raise RuntimeError('Browser cleanup unverified; temporary profile retained') from None
        until=time.monotonic()+2
        while True:
            try: os.killpg(pid,0)
            except ProcessLookupError: break
            except PermissionError:
                pass  # macOS may deny a probe during final group-member exit.
            if time.monotonic()>=until:
                raise RuntimeError('Browser cleanup unverified; temporary profile retained')
            time.sleep(.02)
        shutil.rmtree(profile)


def smoke(app_root):
    """Execute the actual packaged Node/JS/browser trees on about:blank only."""
    import tempfile
    inventory=check(app_root);resource=Path(app_root)/'Contents/Frameworks'
    node=resource/'playwright/driver/node';package=resource/'playwright/driver/package'
    if not node.is_file() or not (package/'index.js').is_file(): raise RuntimeError('Bundled Playwright driver missing')
    if _sha(package/'browsers.json')!=inventory['driver_manifest_sha256']: raise RuntimeError('Bundled Playwright driver identity mismatch')
    profile=tempfile.mkdtemp(prefix='p011-browser-blank-')
    result=_run_owned_smoke(node,('-e',SMOKE_JS,str(package),profile,str(resource/'playwright-browsers')),profile)
    try:
        report=json.loads(result)
        valid=report['closed'] and all(row['version']=='145.0.7632.6' for row in report['browsers'])
    except (ValueError,KeyError,TypeError):
        raise RuntimeError('Bundled browser returned an invalid result') from None
    if not valid:
        raise RuntimeError('Bundled browser version/close check failed')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('preflight','stage','check','smoke'))
    parser.add_argument('--browser-root',type=Path);parser.add_argument('--app-root',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.operation=='preflight': report={'selected':[path.name for path in selected_roots(args.browser_root,_driver_manifest())]}
    elif args.operation=='stage': report=stage(args.browser_root,args.app_root)
    elif args.operation=='check': report=check(args.app_root)
    else: report=smoke(args.app_root)
    if args.report: args.report.write_text(json.dumps(report,indent=2)+'\n')
    else: print(json.dumps({'operation':args.operation,'passed':True}))


if __name__=='__main__': main()
