import copy
import unittest
from unittest.mock import patch

from backend.ai import context_review


def project():
    return {'source_language':'en','target_language':'fr','translation_context':'A threat of revenge, not a debt.',
            'translation_glossary':{},'pages':[{'id':'p1','regions':[
                {'id':'a','source_text':'Run!','target_text':'Courir !','status':'translated','translation_engine':'gemma'},
                {'id':'b','source_text':'Run!','target_text':'Ma correction.','status':'translated','translation_manual':True},
                {'id':'c','source_text':'Run!','target_text':'Relu.','status':'reviewed'}]}]}


class ContextReviewTests(unittest.TestCase):
    def test_review_preserves_human_edits_and_records_machine_draft(self):
        p=project()
        with patch('backend.ai.local_llm.chat',return_value='[{"id":"a","text":"Cours !"}]') as chat:
            result=context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
        self.assertEqual(result['corrected'],1)
        rows=p['pages'][0]['regions']
        self.assertEqual([r['target_text'] for r in rows],['Cours !','Ma correction.','Relu.'])
        self.assertEqual(rows[0]['translation_review']['draft'],'Courir !')
        self.assertIn('A threat of revenge',str(chat.call_args))

    def test_malformed_review_does_not_modify_the_project(self):
        p=project();before=copy.deepcopy(p)
        with patch('backend.ai.local_llm.chat',return_value='[{"id":"wrong","text":"Cours !"}]'):
            with self.assertRaises(ValueError):context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
        self.assertEqual(p,before)

    def test_review_does_not_rewrite_numbers(self):
        p=project();r=p['pages'][0]['regions'][0]
        r.update(source_text='Run for 20 minutes!',target_text='Courir pendant 20 minutes !')
        with patch('backend.ai.local_llm.chat',return_value='[{"id":"a","text":"Cours pendant 200 minutes !"}]'):
            result=context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
        self.assertEqual(result['rejected'],1)
        self.assertEqual(r['target_text'],'Courir pendant 20 minutes !')

    def test_unchanged_review_is_cached_until_context_changes(self):
        p=project()
        with patch('backend.ai.local_llm.chat',return_value='[{"id":"a","text":"Cours !"}]') as chat:
            context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
            context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
            self.assertEqual(chat.call_count,1)
            p['translation_context']='The speaker addresses a group.'
            context_review.review_project(p,['p1'],{'model':'review','model_key':'qwen'})
            self.assertEqual(chat.call_count,2)


if __name__=='__main__':unittest.main()
