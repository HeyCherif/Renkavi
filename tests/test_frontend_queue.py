import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node required for frontend behavior checks')
class QueueNavigationTests(unittest.TestCase):
    def probe(self, status):
        source=(Path(__file__).resolve().parents[1]/'frontend/app.js').read_text(encoding='utf8')
        function=source[source.index('async function pollJob(reference)'):source.index("$('process-all').addEventListener")]
        harness="""
const vm=require('node:vm');
let timers=0,refreshed=0,cleared=0;
const state={project:{project_id:'other-document'},processing:true};
const job={state:STATUS};
const context={state,request:async()=>({job}),lockEditor:value=>state.processing=value,
 setTimeout:()=>timers++,refreshBatches:()=>refreshed++,localStorage:{removeItem:()=>cleared++}};
vm.createContext(context);vm.runInContext(FUNCTION,context);
context.pollJob({project_id:'original-document',id:'job'}).then(()=>console.log(JSON.stringify({timers,refreshed,cleared,processing:state.processing,project:state.project.project_id})));
""".replace('STATUS',json.dumps(status)).replace('FUNCTION',json.dumps(function))
        result=subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=10,check=True)
        return json.loads(result.stdout)

    def test_navigation_does_not_abandon_running_job_poll(self):
        self.assertEqual(self.probe('running')['timers'],1)

    def test_finished_job_unlocks_without_reopening_previous_document(self):
        result=self.probe('completed')
        self.assertFalse(result['processing'])
        self.assertEqual(result['project'],'other-document')
        self.assertEqual(result['cleared'],1)

    def test_navigation_during_final_fetch_preserves_new_document_and_edits(self):
        source=(Path(__file__).resolve().parents[1]/'frontend/app.js').read_text(encoding='utf8')
        function=source[source.index('async function pollJob(reference)'):source.index("$('process-all').addEventListener")]
        harness="""
const vm=require('node:vm');let calls=0;
const state={project:{project_id:'a'},processing:true,dirty:false};
const element={classList:{toggle(){}},textContent:'',value:0,hidden:false};
const context={state,$:()=>element,document:{querySelector:()=>element,querySelectorAll:()=>[]},
 request:async()=>{if(++calls===1)return {job:{state:'completed',stages:[],warnings:[]}};
 state.project={project_id:'b'};state.dirty=true;return {project:{project_id:'a'}};},
 lockEditor:x=>state.processing=x,refreshBatches(){},setTimeout(){},localStorage:{removeItem(){}},
 updateProjectMeta(){},renderPages(){},renderPage(){},flash(){}};
vm.createContext(context);vm.runInContext(FUNCTION,context);
context.pollJob({project_id:'a',id:'job'}).then(()=>console.log(JSON.stringify(state)));
""".replace('FUNCTION',json.dumps(function))
        output=subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=10,check=True)
        result=json.loads(output.stdout)
        self.assertEqual(result['project']['project_id'],'b')
        self.assertTrue(result['dirty'])
