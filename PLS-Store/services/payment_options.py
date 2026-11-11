"""Enabled Platega methods and the buyer-paid rates configured by the merchant."""
METHOD_FEES = {2: '8.5', 13: '5'}
METHOD_LABELS = {
    'ru': {2: 'СБП (QR-код)', 13: 'Криптовалюта'},
    'uk': {2: 'СБП (QR-код)', 13: 'Криптовалюта'},
    'en': {2: 'SBP (QR code)', 13: 'Cryptocurrency'},
}


def method_info(method: int, lang: str = 'ru'):
    if method not in METHOD_FEES:
        raise ValueError('Unsupported payment method')
    label = METHOD_LABELS.get(lang, METHOD_LABELS['ru'])[method]
    fee = METHOD_FEES[method]
    return label, fee.replace('.', ',') if lang in {'ru', 'uk'} else fee
