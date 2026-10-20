import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.ai import local_llm, registry


class ModelSelectionTests(unittest.TestCase):
    def test_missing_gguf_is_unavailable_even_when_lmstudio_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);cli=root/'lms.exe';cli.touch()
            model={'adapter':'lmstudio','path':str(root/'missing.gguf'),'config':{'lms_cli':str(cli)}}
            self.assertFalse(registry.is_available(model))
    def test_background_install_keeps_selection_and_does_not_load_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);file=root/'translator.gguf';file.write_bytes(b'GGUFfixture')
            initial=registry.read_catalog(root)['selected'].copy()
            original_is_file=Path.is_file
            def exists(path):return path.name=='lms.exe' or original_is_file(path)
            def run(command,**kwargs):
                if 'ls' in command:return MagicMock(returncode=0,stdout='[{"modelKey":"renkavi/translator"}]')
                return MagicMock(returncode=0)
            with patch.object(Path,'is_file',autospec=True,side_effect=exists), \
                 patch.object(registry.subprocess,'run',side_effect=run), \
                 patch.object(local_llm,'translate',side_effect=AssertionError('Background installation must not run inference')):
                result=registry.add_model('translation',str(file),root,activate=False,test=False)
                self.assertEqual(result['selected'],initial)
                self.assertFalse(result['models'][-1]['tested'])
                self.assertEqual(registry.add_model('translation',str(file),root,activate=False,test=False)['selected'],initial)
    def test_status_does_not_name_hy_mt_when_gemma_is_selected(self):
        from backend.ai.engines import model_status
        models={'translation':{'name':'TranslateGemma 4B','adapter':'lmstudio'},
                'inpainting':{'name':'Anime LaMa','adapter':'anime_lama_torch'}}
        with patch.object(registry,'selected_model',side_effect=lambda kind:models[kind]), \
             patch.object(registry,'is_available',return_value=True):
            status=model_status()
        self.assertEqual(status['translation']['name'],'TranslateGemma 4B')
        self.assertNotIn('HY-MT2',status['device'])

    def test_catalog_initialization_and_explicit_choice_survive_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = registry.read_catalog(root)
            self.assertTrue((root / "registry.json").is_file())
            registry.select_model("inpainting", "telea", root)
            # Discovering another checkpoint later must not change an explicit choice.
            (root / "inpainting").mkdir()
            (root / "inpainting/lama_large_512px.ckpt").touch()
            self.assertEqual(registry.read_catalog(root)["selected"]["inpainting"], "telea")
            self.assertEqual(first["selected"]["translation"], "argos-en-fr")

    def test_explicit_translation_model_does_not_read_legacy_qwen(self):
        config = {"base_url": "http://127.0.0.1:1234", "model": "renkavi-hy-mt2", "model_key": "hy-mt2-1-8b"}
        model = {"adapter": "lmstudio", "config": config}
        with patch.object(registry, "selected_model", side_effect=AssertionError("Global selection should not be read")):
            self.assertEqual(local_llm.configuration(model)["model_key"], "hy-mt2-1-8b")
            self.assertIsNone(local_llm.configuration({"adapter": "argos"}))

    def test_reserved_port_uses_new_endpoint_and_only_loads_selected_model(self):
        config = {"base_url": "http://127.0.0.1:1234", "model": "renkavi-hy-mt2", "model_key": "hy-mt2-1-8b"}
        with tempfile.TemporaryDirectory() as temporary:
            cli = Path(temporary) / "lms.exe"
            cli.touch()
            config["lms_cli"] = str(cli)
            blocked = MagicMock()
            blocked.__enter__.return_value.bind.side_effect = PermissionError("reserved")
            free = MagicMock()
            free.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 22345)
            with patch.object(local_llm, "CONFIG", Path(temporary) / "llm.json"), \
                 patch.object(local_llm, "loaded", side_effect=[False, True]), \
                 patch.object(local_llm.urllib.request, "urlopen", side_effect=ConnectionRefusedError()), \
                 patch.object(local_llm.socket, "socket", side_effect=[blocked, free]), \
                 patch.object(local_llm.subprocess, "run", return_value=MagicMock(returncode=0)) as run, \
                 patch.object(registry, "save_endpoint") as save:
                local_llm.ensure_loaded(config)
            save.assert_called_once()
            self.assertEqual(config["base_url"], "http://127.0.0.1:22345")
            commands = [call.args[0] for call in run.call_args_list if call.args[0][1:3]!=['server','status']]
            self.assertIn("22345", commands[0])
            self.assertIn("hy-mt2-1-8b", commands[1])
            self.assertNotIn("qwen", json.dumps(commands).lower())

    def test_review_loader_reuses_live_port_and_respects_memory_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            cli=Path(temporary)/'lms.exe';cli.touch()
            config={'base_url':'http://127.0.0.1:6598','model':'renkavi-review','model_key':'qwen/qwen3.5-9b',
                    'lms_cli':str(cli),'context_length':4096,'gpu_offload':.72}
            with patch.object(local_llm,'CONFIG',Path(temporary)/'llm.json'),patch.object(local_llm,'loaded',side_effect=[False,True]),patch.object(local_llm.urllib.request,'urlopen',return_value=MagicMock()),patch.object(registry,'save_endpoint') as save,patch.object(local_llm.subprocess,'run',return_value=MagicMock(returncode=0,stdout='{"running":true,"port":1234}')) as run:
                local_llm.ensure_loaded(config)
            save.assert_called_once()
            self.assertEqual(config['base_url'],'http://127.0.0.1:1234')
            commands=[call.args[0] for call in run.call_args_list]
            self.assertFalse(any(cmd[1:3]==['server','start'] for cmd in commands))
            load=commands[-1]
            self.assertEqual(load[load.index('--gpu')+1],'0.72')
            self.assertEqual(load[load.index('-c')+1],'4096')

    def test_loader_error_names_the_selected_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            cli = Path(temporary) / "lms.exe"
            cli.touch()
            config = {"base_url": "http://127.0.0.1:22345", "model": "renkavi-hy-mt2", "model_key": "hy-mt2-1-8b", "lms_cli": str(cli)}
            with patch.object(local_llm, "CONFIG", Path(temporary) / "llm.json"), \
                 patch.object(local_llm, "loaded", return_value=False), \
                 patch.object(local_llm.urllib.request, "urlopen", return_value=MagicMock()), \
                 patch.object(local_llm.subprocess, "run", return_value=MagicMock(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, "hy-mt2-1-8b"):
                    local_llm.ensure_loaded(config)


if __name__ == "__main__":
    unittest.main()
