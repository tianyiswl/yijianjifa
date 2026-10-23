"""Native Finder drop grants; never expose pasteboard paths to JavaScript."""
from functools import lru_cache
import json


@lru_cache(maxsize=1)
def _type():
    import AppKit
    from webview.platforms.cocoa import BrowserView

    class P011DropView(BrowserView.WebKitHost):
        owner = None

        def draggingEntered_(self, sender):
            owner=P011DropView.owner
            return AppKit.NSDragOperationCopy if owner and owner.trusted==self and sender.draggingSource() is None else AppKit.NSDragOperationNone

        def draggingUpdated_(self, sender):
            return self.draggingEntered_(sender)

        def prepareForDragOperation_(self, sender):
            return bool(self.draggingEntered_(sender))

        def performDragOperation_(self, sender):
            owner=P011DropView.owner
            if not owner or owner.trusted!=self or sender.draggingSource() is not None:return False
            try:
                urls=sender.draggingPasteboard().readObjectsForClasses_options_(
                    [AppKit.NSURL],{AppKit.NSPasteboardURLReadingFileURLsOnlyKey:True}) or []
                paths=[str(url.path()) for url in urls if url.isFileURL()]
                token=owner.registry.grant(paths)
                point=self.convertPoint_fromView_(sender.draggingLocation(),None)
                y=point.y if self.isFlipped() else self.bounds().size.height-point.y
                payload=json.dumps({'token':token,'x':point.x,'y':y})
                self.evaluateJavaScript_inFrame_inContentWorld_completionHandler_(
                    'void window.p011Drop('+payload+')',None,owner.bridge._world,lambda value,error:None)
                return True
            except Exception:
                return False

    import objc
    objc.classAddMethods(BrowserView.WebKitHost,[P011DropView.__dict__[name] for name in ('draggingEntered_','draggingUpdated_','prepareForDragOperation_','performDragOperation_')])
    return P011DropView


class CocoaDropTransport:
    def __init__(self, registry):
        from webview.platforms.cocoa import BrowserView
        self.registry=registry;self.trusted=None;self.bridge=None
        cls=_type();cls.owner=self

    def bind(self,window,bridge):
        import AppKit
        from webview.platforms.cocoa import BrowserView
        self.trusted=BrowserView.instances[window.uid].webview
        self.bridge=bridge
        self.trusted.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])

    def close(self):
        from webview.platforms.cocoa import BrowserView
        self.registry.close();self.trusted=None;self.bridge=None
        _type().owner=None
