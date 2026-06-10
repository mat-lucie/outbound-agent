"""Tests for models/campaign.py — enums, message templates, personalization."""

from models.campaign import (
    DM_STEP_NUMBER,
    Language,
    MessageStep,
    Persona,
    get_message,
    load_messages,
    load_personas,
    load_targets,
    personalize,
)


class TestEnums:
    def test_persona_count(self):
        assert len(Persona) >= 3

    def test_three_languages(self):
        assert len(Language) == 3

    def test_four_message_steps(self):
        assert len(MessageStep) == 4

    def test_dm_step_numbers(self):
        assert DM_STEP_NUMBER[MessageStep.CONNECTION_NOTE] == 0
        assert DM_STEP_NUMBER[MessageStep.DM1] == 1
        assert DM_STEP_NUMBER[MessageStep.DM2] == 2
        assert DM_STEP_NUMBER[MessageStep.DM3] == 3


class TestLoadData:
    def test_load_messages_has_all_personas(self):
        messages = load_messages()
        for persona in Persona:
            assert persona.value in messages

    def test_load_messages_has_all_steps(self):
        messages = load_messages()
        for persona in Persona:
            for step in MessageStep:
                assert step.value in messages[persona.value]

    def test_load_messages_has_at_least_spanish(self):
        """All personas must have at least Spanish messages for every step."""
        messages = load_messages()
        for persona in Persona:
            for step in MessageStep:
                assert "es" in messages[persona.value][step.value]

    def test_enterprise_personas_have_all_languages(self):
        """Enterprise personas have es/en/pt for all steps."""
        messages = load_messages()
        enterprise = ["digitalization_champions", "operations_leaders", "executive_sponsors"]
        for p in enterprise:
            for step in MessageStep:
                for lang in Language:
                    assert lang.value in messages[p][step.value]

    def test_all_message_variants_exist(self):
        """Every persona has at least es messages for all 4 steps."""
        count = 0
        for p in Persona:
            for s in MessageStep:
                msg = get_message(p, Language.ES, s)
                assert msg
                count += 1
        assert count == len(Persona) * len(MessageStep)

    def test_load_personas_has_at_least_three(self):
        personas = load_personas()
        assert len(personas) >= 3
        assert "digitalization_champions" in personas
        assert "operations_leaders" in personas
        assert "executive_sponsors" in personas

    def test_load_targets_has_54(self):
        targets = load_targets()
        assert len(targets) == 54


class TestGetMessage:
    def test_connection_note_has_placeholders(self):
        msg = get_message(Persona.DIGITALIZATION_CHAMPIONS, Language.EN, MessageStep.CONNECTION_NOTE)
        assert "[Name]" in msg
        assert "[Company]" in msg

    def test_dm1_exists(self):
        msg = get_message(Persona.OPERATIONS_LEADERS, Language.ES, MessageStep.DM1)
        assert len(msg) > 50

    def test_dm3_is_soft_close(self):
        msg = get_message(Persona.EXECUTIVE_SPONSORS, Language.EN, MessageStep.DM3)
        assert "last message" in msg.lower()

    def test_operations_leaders_dm2_starts_with_name_prefix(self):
        """operations_leaders DM2 must start with [Name], for all three languages."""
        for lang in Language:
            msg = get_message(Persona.OPERATIONS_LEADERS, lang, MessageStep.DM2)
            assert msg.startswith("[Name], "), f"Language {lang.value}: message does not start with '[Name], '"


class TestPersonalize:
    def test_replaces_name_and_company(self):
        result = personalize("Hi [Name] at [Company]", "John", "Acme")
        assert result == "Hi John at Acme"

    def test_replaces_multiple_occurrences(self):
        result = personalize("[Name] works at [Company]. [Company] is great.", "Ana", "BASF")
        assert result == "Ana works at BASF. BASF is great."

    def test_empty_name(self):
        result = personalize("Hi [Name]", "", "Acme")
        assert result == "Hi "

    def test_no_placeholders(self):
        result = personalize("Plain text message", "John", "Acme")
        assert result == "Plain text message"
