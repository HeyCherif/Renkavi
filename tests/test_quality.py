import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from backend.ai.engines import LocalEngines, group_lines
from backend.ai import local_llm


class QualityTests(unittest.TestCase):
    def test_chinese_lettering_wraps_without_spaces(self):
        from backend.ai.typography import render,balanced_lines,font_for
        text='这是一个没有空格的中文翻译测试用于检查文字换行'
        lines=balanced_lines(text,font_for('cjk-zh',24),150)
        self.assertGreater(len(lines),1)
        self.assertEqual(''.join(lines),text)
        image=Image.new('RGB',(300,200),'white')
        output,warnings=render(image,[{'id':'zh','source_text':'SOURCE','target_text':text,'status':'translated','box':[20,20,280,180]}])
        self.assertFalse(warnings)
        self.assertTrue(np.any(np.asarray(image)!=np.asarray(output)))
    def test_english_ocr_is_repaired_without_changing_numbers(self):
        self.assertTrue(hasattr(local_llm, 'normalize_source'), 'OCR normalization is missing')
        self.assertEqual(local_llm.normalize_source('WHERE 1 WAS BORN. ROSAR10. 3 YEARS.', 'en'), 'Where I was born. Rosario. 3 years.')

    def test_fragment_on_following_line_is_joined(self):
        lines=[{'box':[50,10,220,40], 'text':'WHERE AM', 'score':.99},
               {'box':[100,80,175,110], 'text':'I...?', 'score':.99}]
        self.assertEqual(len(group_lines(lines)),1)

    def test_dark_textured_background_uses_neural_restoration(self):
        arr=np.zeros((260,500,3),np.uint8)+30
        arr += (np.indices(arr.shape[:2])[1] % 13)[...,None].astype(np.uint8)
        image=Image.fromarray(arr);ImageDraw.Draw(image).text((120,95),'THE SLUMS',fill='white',font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf',30))
        engine=LocalEngines()
        def restore(rgb,mask):
            engine.inpaint_method='anime_lama_torch'
            return rgb.copy()
        region={'id':'one','box':[120,95,310,130],'lines':[{'box':[120,95,310,130]}],'status':'translated','target_text':'Les taudis','source_text':'THE SLUMS'}
        with patch.object(engine,'_lama_crop',side_effect=restore) as neural:
            cleaned,mask,counts=engine.clean(image,[region])
        self.assertTrue(neural.called,'Textured dark art must not be filled with its median color')
        self.assertEqual(counts['flat'],0)
        self.assertTrue(np.array_equal(np.asarray(cleaned)[np.asarray(mask)==0],np.asarray(image)[np.asarray(mask)==0]))

    def test_source_typography_is_measured(self):
        image=Image.new('RGB',(420,150),'white')
        ImageDraw.Draw(image).text((25,30),'HELLO WORLD',fill=(10,100,160),font=ImageFont.truetype('C:/Windows/Fonts/comicbd.ttf',32))
        region={'id':'font','box':[25,36,280,68],'source_text':'HELLO WORLD','lines':[{'text':'HELLO WORLD','box':[25,36,280,68]}]}
        from backend.ai import typography
        style=typography.analyze_style(image,region)
        self.assertTrue(style['uppercase'])
        self.assertGreater(style['fill'][2],style['fill'][0]+50)
        self.assertNotEqual(style['font'],'arial')

    def test_numbered_translations_reject_missing_and_duplicate_markers(self):
        self.assertEqual(local_llm.parse_numbered('[1] Un\n[2] Deux',2),['Un','Deux'])
        for text in ['[1] Un [1] Deux','[1] Un','Explication [1] Un [2] Deux']:
            with self.assertRaises(ValueError):local_llm.parse_numbered(text,2)

    def test_lettering_stays_inside_zone(self):
        from backend.ai.typography import render
        image=Image.new('RGB',(500,200),'white')
        region={'id':'fit','source_text':'SOURCE','target_text':'Une traduction française avec des accents É È À',
                'status':'translated','box':[80,40,400,160],
                'auto_style':{'font':'comic','uppercase':True,'fill':[10,80,150],'ink_height':32,'line_count':3}}
        result,warnings=render(image,[region])
        self.assertEqual(warnings,[])
        changed=np.any(np.asarray(result)!=np.asarray(image),axis=2)
        ys,xs=np.where(changed)
        self.assertTrue(len(xs)>0)
        self.assertGreaterEqual(xs.min(),80);self.assertLess(xs.max(),400)
        self.assertGreaterEqual(ys.min(),40);self.assertLess(ys.max(),160)

    def test_glossary_also_applies_after_ocr_repair(self):
        region={'id':'a','source_text':'A ROSAR10.','target_text':''}
        with patch.object(local_llm,'ensure_loaded'),patch.object(local_llm,'chat',return_value='[1] Un rosario.'):
            local_llm.translate([region],'en','fr',{'model':'test','model_key':'test','profile':'hy_mt','glossary':{'rosario':'rosaire'}})
        self.assertEqual(region['target_text'],'Un rosaire.')

    def test_translategemma_uses_its_translation_template(self):
        self.assertTrue(hasattr(local_llm,'gemma_prompt'))
        prompt=local_llm.gemma_prompt('Hello.','en','fr')
        self.assertIn('professional English (en) to French (fr) translator',prompt)
        self.assertIn('Hello.<end_of_turn>\n<start_of_turn>model\n',prompt)

    def test_gemma_keeps_neighboring_regions_aligned_and_applies_glossary(self):
        regions=[{'id':'a','source_text':'A ROSAR10.','target_text':''},
                 {'id':'b','source_text':'IT IS PRICELESS.','target_text':''}]
        with patch.object(local_llm,'ensure_loaded'),patch.object(local_llm,'complete',return_value='[1] Un rosario.\n[2] Il est inestimable.') as complete:
            local_llm.translate(regions,'en','fr',{'model':'test','model_key':'test','profile':'translategemma','glossary':{'rosario':'rosaire'}})
        self.assertEqual([r['target_text'] for r in regions],['Un rosaire.','Il est inestimable.'])
        self.assertEqual(complete.call_count,1)

    def test_gemma_supports_automatic_source_on_english_and_chinese_pages(self):
        for source,language in [('Hello world.','English'),('你好，世界。','Chinese')]:
            region={'id':'a','source_text':source,'target_text':''}
            with patch.object(local_llm,'ensure_loaded'),patch.object(local_llm,'complete',return_value='[1] Bonjour.') as complete:
                local_llm.translate([region],'auto','fr',{'model':'test','model_key':'test','profile':'translategemma'})
            self.assertEqual(region['target_text'],'Bonjour.')
            self.assertIn('professional '+language,complete.call_args.args[1])


if __name__=='__main__':unittest.main()
