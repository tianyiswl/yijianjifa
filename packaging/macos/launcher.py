"""Frozen Mac entry point; avoids executing package-relative modules as scripts."""
import sys

if __name__ == '__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--session-check-worker':
        from yijianjifa.webui.session_check_process import worker_main
        raise SystemExit(worker_main(sys.argv[2:]))
    elif len(sys.argv)>=3 and sys.argv[1]=='--session-browser-gate':
        from yijianjifa.webui.session_check_process import browser_gate_main
        raise SystemExit(browser_gate_main(sys.argv[2:]))
    elif len(sys.argv)>=2 and sys.argv[1]=='--yjjf-ocr-worker':
        from yijianjifa.features.downloads.ocr_worker import worker_main
        raise SystemExit(worker_main(sys.argv[2:]))
    elif len(sys.argv)==2 and sys.argv[1]=='--media-worker':
        from yijianjifa.os_integration.media_guard import worker_main
        raise SystemExit(worker_main())
    elif len(sys.argv)==3 and sys.argv[1]=='--bundle-check':
        from yijianjifa.webui.bundle_check import check
        check(sys.argv[2])
    else:
        from yijianjifa.webui.mac_app import main
        raise SystemExit(main())
