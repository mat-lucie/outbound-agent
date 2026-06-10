"""Test the keyword-based defensive classification fallback in quality_gate."""

from workflows.quality_gate import classify_response


def test_patricia_reply_classified_as_defensive():
    """The canonical Patricia Ejemplo reply must classify as defensive, not neutral."""
    reply = "Hola Andrés no tenemos plantas de producción en Mexico. Pero estoy segura que no viven en Excel con toda certeza. Saludos"
    result = classify_response(reply)
    assert result["classification"] == "defensive", (
        f"Expected 'defensive', got '{result['classification']}'. "
        f"This is the exact reactance pattern the classifier must catch."
    )


def test_english_emphatic_refutation_is_defensive():
    reply = "Actually we don't have that problem. That's not accurate for us."
    result = classify_response(reply)
    assert result["classification"] == "defensive"


def test_portuguese_refutation_is_defensive():
    reply = "Isso não se aplica ao nosso caso em absoluto."
    result = classify_response(reply)
    assert result["classification"] == "defensive"


def test_positive_reply_still_positive():
    """Regression: adding 'defensive' must not break existing positive classification."""
    reply = "Me interesa mucho, cuéntame más sobre la plataforma"
    result = classify_response(reply)
    assert result["classification"] == "positive"


def test_negative_reply_still_negative():
    """Regression: 'not interested' must still classify as negative, not defensive."""
    reply = "No gracias, no me interesa en este momento."
    result = classify_response(reply)
    assert result["classification"] == "negative"


def test_neutral_polite_ack_still_neutral():
    """Regression: polite acknowledgments must still classify as neutral."""
    reply = "Hola, gracias por el mensaje. Saludos."
    result = classify_response(reply)
    assert result["classification"] == "neutral"
