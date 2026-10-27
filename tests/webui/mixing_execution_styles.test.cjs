// Source-level cascade guard; native geometry is verified separately in the app.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const assets=path.resolve(__dirname,'../../src/yijianjifa/webui/assets');
const html=()=>fs.readFileSync(path.join(assets,'mixing.html'),'utf8');
const shared=()=>fs.readFileSync(path.join(assets,'common.css'),'utf8');
function declarations(css,selector){
 const rule=[...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(match=>match[1].trim()===selector);assert(rule,'missing shared rule: '+selector);
 return Object.fromEntries(rule[2].split(';').filter(Boolean).map(text=>{const i=text.indexOf(':');return [text.slice(0,i).trim(),text.slice(i+1).trim()];}));
}
test('the single shared mixing execution host has no stronger legacy progress or button overrides',()=>{
 const source=html();assert.match(source,/<section[^>]+class="[^"]*p011-execution-scope[^"]*"[^>]+id="quick-home"/);
 assert.equal([...source.matchAll(/class="[^"]*\bdaily-progress\b[^"]*"/g)].length,1);
 assert.match(source,/<aside[^>]+class="mixing-execution-panel">[\s\S]*?<div class="daily-progress" id="daily-progress">/);
 const styles=[...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(match=>match[1]).join('\n');
 const overrides=[...styles.matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap(match=>match[1].split(',').map(selector=>selector.trim())).filter(selector=>/^#quick-home\s+\.daily-progress(?:\s+button)?$/.test(selector));
 assert.deepEqual(overrides,[],'legacy ID selectors would override the shared neutral execution styling');
});
test('the shared execution container and action buttons retain their neutral original spacing',()=>{
 const css=shared(),progress=declarations(css,'.p011-execution-scope .mixing-execution-panel .daily-progress');
 assert.equal(progress.margin,'0');assert.equal(progress.padding,'0');assert.equal(progress.background,'none');assert.equal(progress['line-height'],'1.7');
 for(const selector of ['.p011-execution-scope .daily-progress .mixing-execution-account-actions button','.p011-execution-scope .daily-progress .mixing-execution-actions button'])assert.equal(declarations(css,selector).margin,'0');
});
