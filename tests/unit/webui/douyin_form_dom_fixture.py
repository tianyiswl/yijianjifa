"""Local rich-text DOM for the real Douyin preparation script.

The caller owns navigation and must fulfill every request locally.  This helper
does not navigate, choose files, call platform APIs, or synthesize readiness.
Native contenteditable input and a candidate click produce the observed DOM.
"""


def install_form(page, *, topics: list[str], upload_complete=True):
    """Install an empty video form with native controls and topic candidates.

    The intended request has a nonempty body, these topic names, an empty
    declaration, a scheduled time, and no location.  The caller reads the real
    preparation script's result; this fixture does not return a ready result.
    """
    html="""<!doctype html><html><body>
      <input placeholder="填写作品标题，为作品获得更多流量">
      <div id="fixture-editor" contenteditable="true" style="min-height:40px;width:600px">
      </div>
      <div id="fixture-topic-candidates"></div>
      <span>请选择自主声明</span>
      <label><input type="radio" name="schedule" checked>立即发布</label>
      <label><input type="radio" name="schedule">定时发布</label>
      <input placeholder="日期和时间">
      <label><input type="radio" name="audience" checked>公开</label>
      <div id="douyin_creator_pc_anchor_jump">
        <div class="semi-select-filterable semi-select-no-arrow">
          <span class="semi-select-selection-text">输入地理位置</span>
        </div>
      </div>
      <button id="fixture-final" type="button">发布</button>
      <span>上传成功</span>
    </body></html>"""
    if not upload_complete:html=html.replace('<span>上传成功</span>', '')
    page.set_content(html)
    page.evaluate("""fixture => {
      const editor=document.getElementById('fixture-editor');
      const leaf=document.createElement('span');leaf.dataset.string='true';
      // A zero-width leaf is also how a rich editor retains an empty text node.
      leaf.textContent='\u200b';editor.replaceChildren(leaf);
      const candidates=document.getElementById('fixture-topic-candidates');
      for(const name of fixture.topics){
        const candidate=document.createElement('div');
        candidate.className='tag-hash-view-name-fixture';candidate.textContent=name;
        candidate.addEventListener('click',()=>{
          const leaves=Array.from(editor.querySelectorAll('[data-string]:not([data-enter])'))
            .filter(e=>!e.closest('[contenteditable="false"]'));
          const node=leaves.at(-1)?.lastChild;
          if(!node||node.nodeType!==Node.TEXT_NODE||!node.textContent.endsWith('#'+name))
            throw new Error('candidate requires its typed topic suffix');
          node.textContent=node.textContent.slice(0,-('#'+name).length);
          const mention=document.createElement('span');mention.dataset.mention='#';
          mention.contentEditable='false';mention.textContent='#'+name;
          const tail=document.createElement('span');tail.dataset.string='true';tail.textContent=' ';
          editor.append(mention,tail);editor.blur();
        });
        candidates.append(candidate);
      }
      window.p011TopicsInitialized=false;window.p011TopicCursor=null;window.p011TopicWait=null;
      window.p011FinalClicked=false;window.p011FixtureFinalClicks=0;
      document.getElementById('fixture-final').addEventListener('click',()=>window.p011FixtureFinalClicks++);
      // If the production observer is installed, the visible local toast above
      // supplies its evidence; otherwise this isolated fixture supplies the bit.
      if(!Object.getOwnPropertyDescriptor(window,'p011UploadCompleted'))
        Object.defineProperty(window,'p011UploadCompleted',{value:fixture.upload_complete,writable:true,configurable:true});
      else if(Object.getOwnPropertyDescriptor(window,'p011UploadCompleted').writable)
        window.p011UploadCompleted=fixture.upload_complete;
    }""", {"topics": topics, "upload_complete": upload_complete})
    if page.evaluate("window.p011UploadCompleted===true") is not upload_complete:
        raise ValueError("local form upload observation does not match fixture")
