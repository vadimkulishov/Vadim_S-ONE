import unittest
from types import SimpleNamespace

from telegram import MessageEntity
from telegram.constants import MessageEntityType

import main


class CustomEmojiTests(unittest.TestCase):
    def test_placeholder_creates_custom_entity_with_utf16_offsets(self):
        sample = "👨‍👩‍👧‍👦"
        text, entities, changed = main.replace_placeholders(
            "😀 [emoji1] end",
            [],
            [{"id": "5368324170671202286", "sample": sample}],
        )

        self.assertTrue(changed)
        self.assertEqual(text, f"😀 {sample} end")
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, MessageEntityType.CUSTOM_EMOJI)
        self.assertEqual(entities[0].offset, main.u16len("😀 "))
        self.assertEqual(entities[0].length, main.u16len(sample))
        self.assertEqual(entities[0].custom_emoji_id, "5368324170671202286")

    def test_placeholder_preserves_formatting_entity(self):
        source = "😀 [emoji1] end"
        formatting = MessageEntity(
            type=MessageEntityType.BOLD,
            offset=0,
            length=main.u16len(source),
        )

        text, entities, _ = main.replace_placeholders(
            source,
            [formatting],
            [{"id": "5368324170671202286", "sample": "⭐"}],
        )

        self.assertEqual(text, "😀 ⭐ end")
        bold = next(entity for entity in entities if entity.type == MessageEntityType.BOLD)
        custom = next(entity for entity in entities if entity.type == MessageEntityType.CUSTOM_EMOJI)
        self.assertEqual(bold.length, main.u16len(text))
        self.assertEqual(custom.offset, main.u16len("😀 "))

    def test_button_can_use_saved_custom_emoji_as_icon(self):
        keyboard = main.make_keyboard([{
            "text": "Связаться",
            "url": "https://t.me/example",
            "icon_custom_emoji_id": "5368324170671202286",
        }])

        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "Связаться")
        self.assertEqual(button.url, "https://t.me/example")
        self.assertEqual(button.icon_custom_emoji_id, "5368324170671202286")

    def test_button_icon_can_be_selected_by_saved_number_or_message(self):
        saved = [{"id": "saved-id", "sample": "⭐"}]
        by_number = SimpleNamespace(
            text="1", caption=None, entities=[], caption_entities=[]
        )
        self.assertEqual(main.resolve_button_icon(by_number, saved), ("saved-id", "⭐"))

        sample = "💎"
        custom_message = SimpleNamespace(
            text=sample,
            caption=None,
            entities=[MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=0,
                length=main.u16len(sample),
                custom_emoji_id="direct-id",
            )],
            caption_entities=[],
        )
        self.assertEqual(
            main.resolve_button_icon(custom_message, saved),
            ("direct-id", sample),
        )

    def test_button_icon_can_be_omitted(self):
        message = SimpleNamespace(text="-", caption=None, entities=[], caption_entities=[])
        self.assertEqual(main.resolve_button_icon(message, []), (None, None))


if __name__ == "__main__":
    unittest.main()
