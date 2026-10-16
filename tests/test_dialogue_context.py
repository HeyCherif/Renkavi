import unittest
from unittest.mock import patch
from backend.ai.local_llm import gemma_prompt, translate
from backend.ai.context_translation import context_for_window

def regions(texts):
    return [{'id':f'row{i}','source_text':text,'target_text':'','status':'detected'} for i,text in enumerate(texts)]

class DialogueContextTests(unittest.TestCase):
    def test_saved_document_notes_are_sent_to_the_model(self):
        rows=regions(['He is the chosen one.'])
        config={'profile':'translategemma','model':'fake','model_key':'test','base_url':'http://127.0.0.1:1000','document_context':'The narrator is a son; Sumire is his sister.'}
        with patch('backend.ai.local_llm.ensure_loaded'),patch('backend.ai.local_llm.complete',return_value='[1] Il est l’élu.') as complete:
            translate(rows,'en','fr',config,reference_regions=rows)
        self.assertIn('Sumire is his sister',complete.call_args.args[1])
    def test_manual_reply_between_pending_targets_remains_in_context(self):
        rows=regions(['Who killed him?','His brother.','I will avenge him.'])
        rows[1].update(target_text='Son frère.',translation_manual=True,status='reviewed')
        context=context_for_window(rows,[rows[0],rows[2]])
        self.assertIn('His brother.',context)
        self.assertIn('Son frère.',context)
    def test_context_keeps_both_sides_and_distant_character_reference(self):
        opening=regions(['My little sister Sumire and I were left as orphans.'])
        opening[0]['id']='opening'
        rows=regions(['Previous line '+('a'*170),'Earlier dialogue '+('b'*170),'DIE.','Next line establishes the speaker.','Following answer.'])
        context=context_for_window(rows,rows[2:3],chapter_regions=opening+rows,max_chars=850)
        self.assertIn('Sumire',context)
        self.assertIn('Next line establishes',context)
        self.assertLessEqual(len(context),850)

    def test_old_machine_translation_is_not_reused_as_trusted_context(self):
        rows=regions(['I will pay you back double.','I know.'])
        rows[0].update(target_text='Je vous rembourserai le double.',translation_engine='old',translation_context_version=1)
        context=context_for_window(rows,rows[1:])
        self.assertNotIn('rembourserai',context)
        self.assertIn('pay you back',context)

    def test_connector_receives_neighbor_context_and_context_marker(self):
        rows=regions(['It is a threat of revenge.','I will pay you back double.'])
        result={'choices':[{'message':{'content':'[{"id":"row1","text":"Je te le ferai payer au double."}]'}}]}
        config={'profile':'openai_compatible','model':'fake','model_key':'fake','base_url':'https://example.org/v1'}
        with patch('backend.ai.connectors.request',return_value=result) as request:
            translate(rows[1:],'en','fr',config,reference_regions=rows)
        self.assertIn('threat of revenge',str(request.call_args.args[2]))
        self.assertEqual(rows[1].get('translation_context_version'),2)

    def test_previous_translation_and_following_text_context(self):
        rows=regions(['Who was she?', 'His sister.', 'Then why did he lie?', 'Because he is scared.'])
        rows[0]['target_text']='Qui était-elle ?'
        context=context_for_window(rows,rows[1:3],source='en')
        self.assertIn('Qui était-elle',context)
        self.assertIn('AVANT:',context)
        self.assertIn('APRÈS:',context)
        self.assertNotIn('Then why did he lie?',context)
        self.assertIn('Because he is scared.',context)

    def test_gemma_prompt_distinguishes_source_and_read_only_context(self):
        prompt=gemma_prompt('[1] He was lying.','en','fr','AVANT: His sister told him the truth.',True)
        self.assertIn('DO NOT translate',prompt)
        self.assertIn('table cells',prompt)
        self.assertIn('His sister told him the truth.',prompt)
        self.assertIn('[1] He was lying.',prompt)

    def test_gemma_recovers_in_pairs_not_individual_sentences(self):
        rows=regions(['He is lying.','I know it.','Why did she leave?','Because she was scared.'])
        responses=['invalid numbered output','[1] Il ment.\n[2] Je le sais.','[1] Pourquoi est-elle partie ?\n[2] Parce qu’elle avait peur.']
        config={'profile':'translategemma','model':'fake','model_key':'test','base_url':'http://127.0.0.1:1000'}
        with patch('backend.ai.local_llm.ensure_loaded'),patch('backend.ai.local_llm.complete',side_effect=responses) as complete:
            translate(rows,'en','fr',config,reference_regions=rows)
        self.assertEqual(complete.call_count,3)
        self.assertEqual(rows[1]['target_text'],'Je le sais.')
        self.assertEqual(rows[3]['target_text'],'Parce qu’elle avait peur.')
        self.assertNotIn('translation_warnings',rows[0])

    def test_hy_recovers_in_pairs_not_individual_sentences(self):
        rows=regions(['Dont open it.','Why not?','He is still alive.','How is that possible?'])
        config={'profile':'hy_mt','model':'fake','model_key':'fake-hy','base_url':'http://127.0.0.1:1000'}
        replies=['not numbered','[1] Ne l’ouvre pas.\n[2] Pourquoi ?','[1] Il est encore vivant.\n[2] Comment est-ce possible ?']
        with patch('backend.ai.local_llm.ensure_loaded'),patch('backend.ai.local_llm.chat',side_effect=replies) as chat:
            translate(rows,'en','fr',config,reference_regions=rows)
        self.assertEqual(chat.call_count,3)
        self.assertEqual(rows[2]['target_text'],'Il est encore vivant.')
        self.assertEqual(rows[3]['target_text'],'Comment est-ce possible ?')

    def test_context_marker_is_written_only_for_new_translation(self):
        items=regions(['Do not let him leave.','He knows the answer.'])
        config={'profile':'translategemma','model':'fake','model_key':'gemma-test','base_url':'http://127.0.0.1:1000'}
        with patch('backend.ai.local_llm.ensure_loaded'), patch('backend.ai.local_llm.complete',return_value='[1] Ne le laisse pas partir.\n[2] Il connaît la réponse.'):
            translate(items,'en','fr',config,reference_regions=items)
        self.assertTrue(all(row.get('translation_context_version')==2 for row in items))
        self.assertEqual([row['translation_engine'] for row in items],['gemma-test','gemma-test'])

if __name__=='__main__': unittest.main()
