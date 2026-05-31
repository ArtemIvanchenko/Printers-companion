import pytest
from operator_journal.parser import parse_operator_text, _extract_number, OperatorEventDraft
from domain.enums.common import SourceChannel
import re


class TestExtractNumber:
    """Test _extract_number helper function."""

    def test_extract_number_found(self):
        """Test extracting number when pattern matches."""
        pattern = re.compile(r"(\d+(?:[.,]\d+)?)\s*(бар|bar)")
        text = "Потрачено 150 бар"

        value, unit = _extract_number(text, pattern)

        assert value == "150"
        assert unit in ["бар", "bar"]

    def test_extract_number_not_found(self):
        """Test extracting number when pattern doesn't match."""
        pattern = re.compile(r"(\d+)\s*(кг)")
        text = "Без веса"

        value, unit = _extract_number(text, pattern)

        assert value is None
        assert unit is None

    def test_extract_number_invalid_value(self):
        """Test extracting number with invalid value."""
        pattern = re.compile(r"(\d+)\s*(кг)")
        text = "вес = abc кг"

        value, unit = _extract_number(text, pattern)

        assert value is None

    def test_extract_number_comma_decimal(self):
        """Test extracting number with comma decimal."""
        pattern = re.compile(r"(\d+[.,]\d+)\s*(кг)")
        text = "2,5 кг"

        value, unit = _extract_number(text, pattern)

        assert value == "2.5"


class TestParseOperatorText:
    """Test parse_operator_text function."""

    def test_parse_gas_consumption(self):
        """Test parsing gas consumption text."""
        text = "Баллон A1 потрачено 150 бар"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "gas_consumption_recorded"
        assert result.value == "150"
        assert result.unit == "bar"
        assert result.gas_cylinder_id == "A1"

    def test_parse_gas_cylinder_replaced(self):
        """Test parsing gas cylinder replacement text."""
        text = "Баллон B2 заменён"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "gas_cylinder_replaced"
        assert result.gas_cylinder_id == "B2"

    def test_parse_gas_remaining(self):
        """Test parsing gas remaining text."""
        text = "Баллон A1 осталось 50 бар"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "gas_consumption_recorded"
        assert result.value == "50"
        assert result.unit == "bar_remaining"

    def test_parse_powder_consumption_kg(self):
        """Test parsing powder consumption in kg."""
        text = "Порошок batch123 использовано 2.5 кг"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "powder_consumption_recorded"
        assert result.value == "2.5"
        assert result.unit == "kg"
        assert result.powder_batch == "batch123"

    def test_parse_powder_consumption_grams(self):
        """Test parsing powder consumption in grams."""
        text = "Использовано 500 грамм порошка"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "powder_consumption_recorded"
        assert result.value == "0.5"
        assert result.unit == "kg"

    def test_parse_powder_sieved(self):
        """Test parsing powder sieved text."""
        text = "Порошок просеяли через сито"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "powder_sieved"

    def test_parse_powder_dried(self):
        """Test parsing powder dried text."""
        text = "Порошок высушен"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "powder_dried"

    def test_parse_part_accepted(self):
        """Test parsing part accepted text."""
        text = "Деталь принята"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "part_accepted"

    def test_parse_part_rejected(self):
        """Test parsing part rejected text."""
        text = "Деталь забракована"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "part_rejected"

    def test_parse_defect_porosity(self):
        """Test parsing porosity defect text."""
        text = "Обнаружен дефект porosity"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "visual_defect_found"

    def test_parse_filter_replaced(self):
        """Test parsing filter replacement text."""
        text = "Фильтр заменён"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "filter_replaced"
        assert result.component == "filter"

    def test_parse_seal_replaced(self):
        """Test parsing seal replacement text."""
        text = "Уплотнение заменено"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "seal_replaced"
        assert result.component == "seal"

    def test_parse_restart_attempt(self):
        """Test parsing restart attempt text."""
        text = "Рестарт на слое 50"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "restart_attempt"
        assert result.layer == 50

    def test_parse_restart_without_layer(self):
        """Test parsing restart without layer."""
        text = "Произведён рестарт"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "restart_attempt"
        assert result.layer is None

    def test_parse_material_extraction(self):
        """Test material extraction from text."""
        text = "Печать на алюминии AlSi10Mg"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.material == "AlSi10Mg"

    def test_parse_default_observation(self):
        """Test default observation for unknown text."""
        text = "Просто заметка оператора"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.event_type == "operator_observation"
        assert result.note == text

    def test_parse_confidence_gas(self):
        """Test confidence calculation for gas."""
        text = "Баллон A1 потрачено 150 бар"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.confidence >= 0.55

    def test_parse_confidence_high_for_specific(self):
        """Test high confidence for specific events."""
        text = "Баллон A1 заменён, потрачено 150 бар"
        result = parse_operator_text(text, SourceChannel.telegram)

        assert result.confidence >= 0.75
        assert result.verification_status.value == "draft"


class TestOperatorEventDraft:
    """Test OperatorEventDraft model."""

    def test_default_values(self):
        """Test default values."""
        draft = OperatorEventDraft(event_type="test")

        assert draft.event_type == "test"
        assert draft.confidence == 0.4
        assert draft.source_channel == SourceChannel.telegram

    def test_model_validation(self):
        """Test pydantic model validation."""
        data = {
            "event_type": "gas_consumption_recorded",
            "value": "100",
            "unit": "bar",
            "confidence": 0.8,
        }
        draft = OperatorEventDraft(**data)

        assert draft.event_type == "gas_consumption_recorded"
        assert draft.value == "100"