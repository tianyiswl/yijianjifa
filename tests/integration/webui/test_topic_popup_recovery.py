"""Execute the production recovery helper against an editor/selection fixture."""
import subprocess
from yijianjifa.webui.douyin_publish_form import TOPIC_RECOVERY


def test_missing_popup_requeries_only_pending_suffix_with_bounded_retries():
    script = TOPIC_RECOVERY + r'''
const assert=require('node:assert/strict');
global.Node={TEXT_NODE:3};
let inserts=[],focus=0,start,end;
const node={nodeType:3,textContent:'正文 #已确认 #不同视角',get length(){return this.textContent.length}};
const editor={focus(){focus++},querySelectorAll(){return [{closest(){return null},lastChild:node}]}};
const selection={removeAllRanges(){},addRange(){}};
global.window={getSelection(){return selection}};
global.document={createRange(){return {setStart(n,o){start=o},setEnd(n,o){end=o}}},execCommand(command,unused,text){
 assert.equal(command,'insertText');inserts.push(text);
 node.textContent=node.textContent.slice(0,start)+text+node.textContent.slice(end);
}};
let state={last:0,attempts:0};
assert.equal(recoverPendingTopic(editor,'不同视角',state,7999),'waiting');
assert.equal(focus,0);
for(let now of [8000,16000,24000]){
 assert.equal(recoverPendingTopic(editor,'不同视角',state,now),'retry');
 assert.equal(node.textContent,'正文 #已确认 #不同视角');
}
assert.deepEqual(inserts,['角','角','角']);
assert.equal(recoverPendingTopic(editor,'不同视角',state,32000),'failed');
assert.equal(focus,3);
// Changed text must never be overwritten.
node.textContent='用户正在编辑';
assert.equal(recoverPendingTopic(editor,'不同视角',{last:0,attempts:0},8000),'unsafe');
assert.equal(node.textContent,'用户正在编辑');
// Unicode supplementary characters are not split into invalid surrogate halves.
node.textContent='正文 #😀';
assert.equal(recoverPendingTopic(editor,'😀',{last:0,attempts:0},8000),'retry');
assert.equal(inserts.at(-1),'😀');
assert.equal(node.textContent,'正文 #😀');
'''
    subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
