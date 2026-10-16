import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from PIL import Image

from backend.api.server import make_server
from backend.projects.store import ProjectStore


class ApiTests(unittest.TestCase):
    def test_foreign_hosts_and_origins_are_rejected(self):
        self.assertEqual(self.request('GET', '/api/health', headers={'Host': 'attacker.example'})[0], 403)
        headers = {'X-Renkavi-Client': '1', 'Origin': 'http://attacker.example'}
        self.assertEqual(self.request('POST', '/api/workspaces', b'{"title":"foreign"}', headers)[0], 403)
        local = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.request('GET', '/api/health', headers={'Origin': local})[0], 200)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name))
        self.server = make_server(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_list_save_and_conflict(self):
        project = self.store.create("Test", "ja", "fr")
        status, payload = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["projects"][0]["title"], "Test")
        project["title"] = "Modifié"
        body = json.dumps({"project": project, "expected_revision": 1}).encode()
        url = f"/api/projects/{project['project_id']}"
        self.assertEqual(self.request("PUT", url, body, {"Content-Type": "application/json"})[0], 403)
        headers = {"Content-Type": "application/json", "X-Renkavi-Client": "1"}
        self.assertEqual(self.request("PUT", url, body, headers)[0], 200)
        self.assertEqual(self.request("PUT", url, body, headers)[0], 409)

    def test_uploaded_image_and_page_asset(self):
        image = io.BytesIO()
        Image.new("RGB", (32, 40), "white").save(image, format="PNG")
        status, payload = self.request("POST", "/api/import?filename=page.png&source_language=ja&target_language=fr", image.getvalue(), {"X-Renkavi-Client": "1"})
        self.assertEqual(status, 201)
        project = json.loads(payload)["project"]
        self.assertEqual(project["pages"][0]["width"], 32)
        asset = f"/api/projects/{project['project_id']}/pages/{project['pages'][0]['id']}"
        self.assertEqual(self.request("GET", asset)[1], image.getvalue())

    def test_bulk_import_keeps_successes_and_reports_invalid_archive(self):
        import zipfile
        image=io.BytesIO();Image.new('RGB',(32,40),'white').save(image,format='PNG')
        directory=Path(self.temp.name)/'imports';directory.mkdir()
        with zipfile.ZipFile(directory/'Chapitre 2.cbz','w') as archive:archive.writestr('001.png',image.getvalue())
        (directory/'Chapitre 3.cbz').write_bytes(b'broken zip')
        body=json.dumps({'paths':[str(directory)],'source_language':'en','target_language':'fr'}).encode()
        self.assertEqual(self.request('POST','/api/import-batch',body)[0],403)
        status,payload=self.request('POST','/api/import-batch',body,{'X-Renkavi-Client':'1'})
        self.assertEqual(status,200,payload)
        data=json.loads(payload)
        self.assertEqual(len(data['projects']),1)
        self.assertEqual(data['projects'][0]['title'],'Chapitre 2')
        self.assertEqual(len(data['errors']),1)

    def test_batch_routes_reject_empty_selection_and_unknown_history(self):
        headers={'X-Renkavi-Client':'1'}
        self.assertEqual(self.request('GET','/api/batches')[0],200)
        self.assertEqual(self.request('POST','/api/batches',b'{"project_ids":[]}',headers)[0],400)
        self.assertEqual(self.request('GET','/api/batches/missing')[0],404)

    def test_workspace_import_and_settings_persist(self):
        headers={'X-Renkavi-Client':'1'}
        status,payload=self.request('POST','/api/workspaces',json.dumps({'title':'Ma série','source_language':'en','target_language':'es'}).encode(),headers)
        self.assertEqual(status,201,payload)
        workspace=json.loads(payload)['workspace']
        image=io.BytesIO();Image.new('RGB',(40,50),'white').save(image,format='PNG')
        status,payload=self.request('POST',f"/api/import?filename=Chapitre1.png&workspace_id={workspace['id']}&source_language=en&target_language=es",image.getvalue(),headers)
        self.assertEqual(status,201,payload)
        chapter=json.loads(payload)['project']
        self.assertTrue((Path(workspace['folder'])/'chapters'/chapter['project_id']/'project.json').is_file())
        result=json.loads(self.request('GET',f"/api/workspaces/{workspace['id']}")[1])['workspace']
        self.assertEqual(result['chapter_count'],1)
        self.assertEqual(result['chapters'][0]['target_language'],'es')
        status,payload=self.request('PUT','/api/settings',json.dumps({'settings':{'ui_language':'en','export':{'format':'jpeg','quality':88}}}).encode(),headers)
        self.assertEqual(status,200,payload)
        self.assertEqual(json.loads(self.request('GET','/api/settings')[1])['settings']['export']['quality'],88)
        self.assertEqual(self.request('PUT','/api/settings',b'{"settings":{"export":{"quality":999}}}',headers)[0],400)

    def test_model_hub_catalogue_and_real_template_assets(self):
        status,payload=self.request('GET','/api/model-hub')
        self.assertEqual(status,200,payload)
        catalogue=json.loads(payload)
        self.assertTrue(any(model.get('downloadable') for model in catalogue['models']))
        self.assertTrue(all(model.get('size_bytes',0)>0 for model in catalogue['models'] if model.get('downloadable')))
        self.assertEqual(self.request('GET','/vendor/tabler.min.css')[0],200)
        self.assertIn(b'tabler-home',self.request('GET','/vendor/tabler-sprite.svg')[1])
        self.assertEqual(self.request('POST','/api/model-hub/download',b'{"model_id":"unknown"}',{'X-Renkavi-Client':'1'})[0],400)

    def test_cover_uses_real_chapter_and_keeps_original(self):
        from backend.projects.workspaces import Workspaces
        from backend.projects.importer import import_source
        source=Path(self.temp.name)/'page.png';Image.new('RGB',(100,300),(40,80,120)).save(source)
        original=source.read_bytes()
        chapter=import_source(self.store,source,source_language='en',target_language='fr')
        workspaces=Workspaces(self.store);workspace=workspaces.create('Cover test');workspaces.attach(workspace['id'],chapter['project_id'])
        status,payload=self.request('GET',f"/api/workspaces/{workspace['id']}/cover")
        self.assertEqual(status,200,payload)
        with Image.open(io.BytesIO(payload)) as cover:self.assertEqual(cover.size,(480,320))
        self.assertEqual(source.read_bytes(),original)

    def test_variant_routes_require_saved_mask_and_revision(self):
        source = Path(self.temp.name) / "page.png"
        Image.new("RGB", (32, 40), "white").save(source)
        from backend.projects.importer import import_source
        project = import_source(self.store, source, source_language="ja", target_language="fr")
        page = project["pages"][0]
        url = f"/api/projects/{project['project_id']}/pages/{page['id']}/variants"
        self.assertFalse(json.loads(self.request("GET", url)[1])["available"])
        project["pages"][0]["mask_strokes"] = [{"mode": "paint", "radius": 3, "points": [[12, 12]]}]
        self.store.save(project, 1)
        body = json.dumps({"expected_revision": 2}).encode()
        self.assertEqual(self.request("POST", url, body)[0], 403)
        self.assertEqual(self.request("POST", url, json.dumps({"expected_revision": 1}).encode(), {"X-Renkavi-Client": "1"})[0], 409)
        self.assertEqual(self.request("POST", url, body, {"X-Renkavi-Client": "1"})[0], 200)
        self.assertTrue(json.loads(self.request("GET", url)[1])["available"])
        status, payload = self.request("GET", url + "/mask")
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(payload)) as mask:
            self.assertEqual(mask.getpixel((12, 12)), 255)


if __name__ == "__main__":
    unittest.main()
