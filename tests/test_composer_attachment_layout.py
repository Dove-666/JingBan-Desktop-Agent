"""A staged filename must not push real renderer controls outside a small window."""
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest

from tests import test_chat_image_web as fixture
from tests.test_assistant_ui import click_dom,eval_js,set_dom_text,wait_js
from tests.test_glass_ui import GlassInteractions


class ComposerAttachmentLayoutTests(GlassInteractions,unittest.TestCase):
    setUpClass=fixture.ChatImageWebTests.__dict__['setUpClass']
    setUp=fixture.ChatImageWebTests.setUp
    tearDown=fixture.ChatImageWebTests.tearDown
    wait_thumbnail=fixture.ChatImageWebTests.wait_thumbnail

    def test_long_filename_and_long_draft_keep_send_and_remove_controls_reachable(self):
        self.wait_thumbnail()
        # Match the content viewport of the 420x500 native main window, which
        # reserves 8px per edge. Real production controllers expose Agent mode.
        self.panel.resize(404,484)
        self.chat.set_agent_mode=lambda enabled: True
        self.chat.agent_mode=False
        self.chat.reading_font_size=22;self.panel._queue_web_state()
        self.wait_for_renderer_size(self.panel)
        wait_js(self.panel,"Boolean(document.querySelector('.chat-mode-row'))")
        filename='鲸鱼娘拍摄的桌面截图'*12+'.png'
        metadata={'id':'b'*64,'filename':filename,'width':24,'height':16,'size':100}
        with patch.object(self.chat,'import_local_image',lambda path:(metadata,self.chat.image),create=True),\
             patch('desktop_pet.ui.chat_panel.QFileDialog.getOpenFileName',return_value=('C:/SYNTHETIC_LONG_FILENAME.png','')):
            click_dom(self.panel,'[data-testid=composer-image-picker]')
        wait_js(self.panel,"Boolean(document.querySelector('[data-testid=composer-attachment]'))")
        draft='\n'.join('第%d行：保留完整草稿，输入框可以滚动。'%i for i in range(20))
        set_dom_text(self.panel,'#message-input',draft);QTest.qWait(70)
        metrics=eval_js(self.panel,"""(() => {
          const input=document.querySelector('#message-input'),name=document.querySelector('.composer-attachment-details strong');
          return {height:innerHeight,draft:input.value,inputScroll:input.scrollHeight,inputClient:input.clientHeight,
           name:name.textContent,tooltip:name.title,nameHeight:name.getBoundingClientRect().height,
           sendBottom:document.querySelector('[data-testid=send-button]').getBoundingClientRect().bottom};
        })()""")
        self.assertLessEqual(metrics['sendBottom'],metrics['height'],'The send button is clipped outside the window')
        self.assertEqual(draft,metrics['draft']);self.assertGreater(metrics['inputScroll'],metrics['inputClient'])
        self.assertEqual(filename,metrics['name']);self.assertEqual(filename,metrics['tooltip'])
        self.assertLess(metrics['nameHeight'],35,'The filename stays compact while its full name is available')
        self.click_web(self.panel,'[data-testid=remove-composer-image]')
        wait_js(self.panel,"!document.querySelector('[data-testid=composer-attachment]')")
        self.assertEqual(draft,eval_js(self.panel,"document.querySelector('#message-input').value"))
        self.assertEqual([],self.chat.sent)


if __name__=='__main__':unittest.main()
