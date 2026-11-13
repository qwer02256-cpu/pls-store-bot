"""One banner catalogue and resolver shared by customer and administrator screens."""
from config import ASSETS_DIR, BANNERS_DIR, BANNER_PATH
from services.db import get_setting, get_user_language

BANNER_KEYS = ['home', 'profile', 'shop', 'faq', 'topup', 'language', 'plugins',
               'documents', 'support', 'reviews', 'purchases', 'referral',
               'licenses', 'license_detail', 'product', 'checkout', 'payment',
               'success', 'promo', 'api', 'admin']


def resolve_banner(key, lang, settings, assets_dir, uploads_dir, home_path):
    lang = lang if lang in {'ru', 'en', 'uk'} else 'ru'
    name = settings(f'banner_{key}_{lang}', '')
    if not name and lang == 'ru':
        name = settings(f'banner_{key}', '')
    if name and (uploads_dir / name).is_file():
        return uploads_dir / name
    if lang != 'ru' and key in BANNER_KEYS:
        localized = assets_dir / lang / ('banner.png' if key == 'home' else f'{key}.png')
        if localized.is_file():
            return localized
        return resolve_banner(key, 'ru', settings, assets_dir, uploads_dir, home_path)
    if key in BANNER_KEYS and key != 'home':
        bundled = assets_dir / f'{key}.png'
        return bundled if bundled.is_file() else None
    return home_path if key == 'home' and home_path.is_file() else None


def banner_path(key, lang='ru'):
    return resolve_banner(key, lang, get_setting, ASSETS_DIR, BANNERS_DIR, BANNER_PATH)


async def themed_reply(target, text, banner_key=None, **kwargs):
    from services.screens import screen_reply
    if banner_key is None:
        data = getattr(target, 'data', '')
        banner_key = 'license_detail' if isinstance(data, str) and data.startswith('lic:') else 'admin'
    uid = target.from_user.id
    kwargs.setdefault('photo', banner_path(banner_key, get_user_language(uid)))
    return await screen_reply(target, text, **kwargs)
