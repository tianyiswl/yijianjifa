"""Read-only ZIP inventory, duplicate hashes, and native Qt import closure."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('archive',type=Path)
parser.add_argument('output',type=Path)
args=parser.parse_args()
import pefile

with zipfile.ZipFile(args.archive) as z:
    files=[i for i in z.infolist() if not i.is_dir()]
    sizes={i.filename:i.file_size for i in files}
    groups=defaultdict(list)
    for i in files:groups[i.file_size].append(i.filename)
    duplicates=[]
    for size,paths in groups.items():
        if size==0 or len(paths)<2:continue
        hashes=defaultdict(list)
        for path in paths:hashes[hashlib.sha256(z.read(path)).hexdigest()].append(path)
        for digest,items in hashes.items():
            if len(items)>1:duplicates.append(dict(size=size,redundant_bytes=size*(len(items)-1),sha256=digest,paths=items))
    qt={n.split('/PySide6/',1)[1]:n for n in sizes if '/PySide6/' in n}
    native={n:p for n,p in qt.items() if n.lower().endswith(('.dll','.pyd','.exe'))}
    basename=defaultdict(list)
    for n in native:basename[Path(n).name.lower()].append(n)
    imports={};failures=[]
    for name,path in native.items():
        try:
            pe=pefile.PE(data=z.read(path),fast_load=True)
            pe.parse_data_directories(directories=[1,13])
            imports[name]=sorted({entry.dll.decode('ascii').lower() for attr in ['DIRECTORY_ENTRY_IMPORT','DIRECTORY_ENTRY_DELAY_IMPORT'] for entry in getattr(pe,attr,[])})
            pe.close()
        except Exception as error:failures.append(dict(name=name,error=type(error).__name__))
    roots=['QtCore.pyd','QtGui.pyd','QtWidgets.pyd','QtWebChannel.pyd','QtWebEngineCore.pyd','QtWebEngineWidgets.pyd','QtPrintSupport.pyd','QtWebEngineProcess.exe']
    # Qt dynamically discovers these plugin families. Keep every member as a
    # conservative root; QML and multimedia are NOT assumed to be safe to cut.
    roots += [n for n in native if n.startswith(('plugins/platforms/','plugins/imageformats/','plugins/styles/','plugins/iconengines/','plugins/tls/','plugins/networkinformation/','plugins/printsupport/'))]
    reached=set();pending=list(roots)
    while pending:
        name=pending.pop()
        if name in reached or name not in native:continue
        reached.add(name)
        for dependency in imports.get(name,[]):pending.extend(basename.get(dependency,[]))
    direct=[dict(name=n,size=sizes[qt[n]],imports=imports[n]) for n in sorted(reached)]
    outside=[dict(name=n,size=sizes[p],imports=imports.get(n,[])) for n,p in native.items() if n not in reached]
    suffixes=Counter()
    for i in files:suffixes[Path(i.filename).suffix.lower()]+=i.file_size
    report=dict(archive=str(args.archive),file_count=len(files),unpacked_bytes=sum(sizes.values()),
        small_files=dict(count=sum(i.file_size<102400 for i in files),bytes=sum(i.file_size for i in files if i.file_size<102400)),
        duplicates=sorted(duplicates,key=lambda d:d['redundant_bytes'],reverse=True),
        duplicate_redundant_bytes=sum(d['redundant_bytes'] for d in duplicates),
        developer_files=[dict(path=i.filename,size=i.file_size) for i in files if Path(i.filename).suffix.lower() in {'.pdb','.debug','.map','.h','.hpp','.lib','.a','.qmltypes'}],
        qt_import_roots=roots,qt_reachable=direct,qt_outside_native_closure=sorted(outside,key=lambda d:d['size'],reverse=True),
        qt_outside_native_bytes=sum(d['size'] for d in outside),qt_parse_failures=failures,
        by_suffix=dict(suffixes.most_common()),
        note='Outside native closure is a candidate, not proof of unused: dynamic plugin/QML loading requires separate validation.')
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['file_count','unpacked_bytes','small_files','duplicate_redundant_bytes','qt_outside_native_bytes','qt_parse_failures']},ensure_ascii=False))
