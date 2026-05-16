import pyautogui
import cv2
import time
import tkinter as tk
from tkinter import scrolledtext, simpledialog
import threading
import webbrowser
import subprocess
import shutil
import os
import queue
import re
from urllib.parse import urljoin, urlparse, parse_qs
from tkinter import messagebox
import requests
from bs4 import BeautifulSoup

# Crear ventana de logs
ventana = tk.Tk()
ventana.title("Logs del Bot")
ventana.geometry("500x300+0+0")
ventana.attributes("-topmost", True)

log_text = scrolledtext.ScrolledText(ventana, wrap=tk.WORD, bg="#f0f0f0", font=("Arial", 9))
log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
log_text.config(state=tk.DISABLED)

log_queue = queue.Queue()
prompt_queue = queue.Queue()

def agregar_log(mensaje):
	log_queue.put(mensaje)


def process_queue():
	try:
		while True:
			msg = log_queue.get_nowait()
			log_text.config(state=tk.NORMAL)
			log_text.insert(tk.END, msg + "\n")
			log_text.see(tk.END)
			log_text.config(state=tk.DISABLED)
	except queue.Empty:
		pass
	try:
		while True:
			pdf_urls, resultado, evento = prompt_queue.get_nowait()
			if pdf_urls:
				mensaje = "Se han encontrado estos PDFs:\n\n" + "\n".join(f"- {pdf}" for pdf in pdf_urls)
				mensaje += "\n\n¿Se han de descargar?"
				resultado["value"] = messagebox.askyesno("PDFs encontrados", mensaje)
			else:
				resultado["value"] = False
			evento.set()
	except queue.Empty:
		pass
	ventana.after(100, process_queue)


def _es_url_pdf_directa(url):
	"""Devuelve True si la URL apunta claramente a un archivo PDF."""
	url_limpia = url.split("?")[0].split("#")[0].lower()
	return url_limpia.endswith(".pdf")


def _es_recurso_moodle(url):
	"""Detecta enlaces de Moodle que podrían ser PDFs tras una redirección."""
	patrones_moodle = [
		r"/mod/resource/view\.php",
		r"/pluginfile\.php/.*\.pdf",
		r"/mod/folder/",
	]
	return any(re.search(p, url, re.IGNORECASE) for p in patrones_moodle)


def _resolver_redireccion_pdf(url, sesion, timeout=15):
	"""
	Obtiene la página de recurso Moodle y busca el enlace real al PDF.
	Moodle no redirige directamente: sirve una página HTML con un iframe
	o enlace a pluginfile.php que es el archivo real.
	"""
	try:
		# Primero probar HEAD/GET directo por si acaso redirige al PDF
		resp = sesion.head(url, timeout=timeout, allow_redirects=True)
		ct = resp.headers.get("Content-Type", "")
		if "pdf" in ct.lower():
			return resp.url
		if _es_url_pdf_directa(resp.url):
			return resp.url

		# Descargar el HTML de la página del recurso
		resp2 = sesion.get(url, timeout=timeout, allow_redirects=True)
		ct2 = resp2.headers.get("Content-Type", "")
		if "pdf" in ct2.lower():
			return resp2.url
		if _es_url_pdf_directa(resp2.url):
			return resp2.url

		# Buscar en el HTML el enlace a pluginfile.php o enlace directo .pdf
		html = resp2.text
		soup = BeautifulSoup(html, "html.parser")

		#Buscar en todos los atributos href/src/data
		for tag in soup.find_all(True):
			for attr in ("href", "src", "data", "data-url"):
				val = tag.get(attr, "")
				if not val:
					continue
				val_abs = urljoin(url, val)
				if "pluginfile.php" in val_abs and ".pdf" in val_abs.lower():
					return val_abs
				if _es_url_pdf_directa(val_abs):
					return val_abs

		# Buscar pluginfile con cualquier extensión y verificar Content-Type
		for tag in soup.find_all(True):
			for attr in ("href", "src", "data"):
				val = tag.get(attr, "")
				if "pluginfile.php" in val:
					val_abs = urljoin(url, val)
					try:
						r3 = sesion.head(val_abs, timeout=timeout, allow_redirects=True)
						if "pdf" in r3.headers.get("Content-Type", "").lower():
							return r3.url
					except Exception:
						pass

		# Buscar con regex en el HTML crudo por si está en JS o atributos no estándar
		patron = re.compile(
			r'https?://[^\s"\'<>]*pluginfile\.php[^\s"\'<>]*',
			re.IGNORECASE
		)
		for match in patron.findall(html):
			enlace = match.replace("\\u0026", "&").replace("&amp;", "&")
			if ".pdf" in enlace.lower():
				return enlace
			# Verificar Content-Type para pluginfile sin extensión visible
			try:
				r4 = sesion.head(enlace, timeout=timeout, allow_redirects=True)
				if "pdf" in r4.headers.get("Content-Type", "").lower():
					return r4.url
			except Exception:
				pass

	except Exception as e:
		agregar_log(f"[warning] Error resolviendo {url}: {e}")
	return None


def _detectar_moodle_base(course_url):
	"""
	Deduce la URL base de Moodle a partir de una URL de curso.
	"""
	parsed = urlparse(course_url)
	# Segmentos Moodle conocidos que marcan el inicio del path interno
	for marcador in ("/course/", "/mod/", "/login/", "/my/", "/user/"):
		idx = parsed.path.lower().find(marcador)
		if idx != -1:
			subpath = parsed.path[:idx]
			return f"{parsed.scheme}://{parsed.netloc}{subpath}"
	# Fallback: solo scheme + netloc
	return f"{parsed.scheme}://{parsed.netloc}"


def _crear_sesion_moodle(usuario, contraseña, base_url="https://campusvirtual.ull.es"):
	"""
	Hace login en Moodle vía CAS SSO y devuelve una sesión autenticada.
	Retorna (sesion, True) si tuvo éxito, (None, False) si falló.
	"""
	sesion = requests.Session()
	sesion.headers.update({
		"User-Agent": (
			"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
			"AppleWebKit/537.36 (KHTML, like Gecko) "
			"Chrome/124.0.0.0 Safari/537.36"
		)
	})

	base_url = base_url.rstrip("/")

	# Acceder al endpoint CAS de Moodle → redirige al servidor CAS externo
	cas_entry = f"{base_url}/login/index.php?authCAS=CAS"
	agregar_log(f"[info] Iniciando flujo CAS: {cas_entry}")

	try:
		resp1 = sesion.get(cas_entry, timeout=20, allow_redirects=True)
		url_cas = resp1.url
		agregar_log(f"[debug] Servidor CAS: {url_cas}")
		agregar_log(f"[debug] Status: {resp1.status_code}")

		if resp1.status_code != 200:
			agregar_log(f"[warning] Error al acceder al servidor CAS: {resp1.status_code}")
			return None, False

		# Paso 2: Rellenar el formulario de login del servidor CAS
		soup = BeautifulSoup(resp1.text, "html.parser")
		form = soup.find("form")
		if not form:
			agregar_log("[warning] No se encontró formulario en la página CAS")
			agregar_log(f"[debug] HTML recibido (500 chars): {resp1.text[:500]}")
			return None, False

		action = form.get("action", "")
		action_url = urljoin(url_cas, action) if action else url_cas
		agregar_log(f"[debug] Formulario CAS encontrado, action: {action_url}")

		# Recoger TODOS los campos del formulario
		payload = {}
		for inp in form.find_all("input"):
			name = inp.get("name")
			value = inp.get("value", "")
			if name:
				payload[name] = value

		# Detectar e inyectar usuario y contraseña
		campos_usuario = ("username", "user", "j_username", "USER", "loginName", "uid")
		campos_password = ("password", "pass", "j_password", "PASSWORD", "passwd", "credential")

		campo_u = next((c for c in campos_usuario if soup.find("input", {"name": c})), "username")
		campo_p = next((c for c in campos_password if soup.find("input", {"name": c})), "password")

		payload[campo_u] = usuario
		payload[campo_p] = contraseña
		agregar_log(f"[debug] Campos detectados → usuario: '{campo_u}', contraseña: '{campo_p}'")

		# POST al servidor CAS
		agregar_log("[info] Enviando credenciales al servidor CAS...")
		resp2 = sesion.post(action_url, data=payload, timeout=20, allow_redirects=True)
		url_tras_cas = resp2.url
		agregar_log(f"[debug] URL tras POST CAS: {url_tras_cas}")
		agregar_log(f"[debug] Status: {resp2.status_code}")

		# Verificar si el CAS rechazó las credenciales (volvemos a la misma página CAS)
		if urlparse(url_tras_cas).netloc == urlparse(url_cas).netloc:
			soup2 = BeautifulSoup(resp2.text, "html.parser")
			errores = soup2.find(class_=re.compile(r"error|alert|warning|msg", re.I))
			msg_error = errores.get_text(strip=True) if errores else "(sin mensaje de error visible)"
			agregar_log(f"[warning] CAS rechazó las credenciales: {msg_error}")
			return None, False

		# CAS redirige de vuelta a Moodle con un ticket
		if "campusvirtual.ull.es" in url_tras_cas and "login" not in url_tras_cas.lower():
			agregar_log("[info] Sesión CAS autenticada correctamente")
			return sesion, True

		agregar_log(f"[warning] Login CAS no completado. URL final: {url_tras_cas}")
		return None, False

	except Exception as e:
		agregar_log(f"[warning] Error en flujo CAS: {e}")
		return None, False



def extraer_pdfs_desde_pagina(base_url, usuario=None, contraseña=None):
	"""
	Hace login con requests,,
	obtiene la página del curso y extrae todos los enlaces PDF,
	incluyendo recursos Moodle que redirigen a PDFs.
	"""
	# Login HTTP directo
	moodle_base = _detectar_moodle_base(base_url)
	agregar_log(f"[debug] Moodle base detectada: {moodle_base}")
	sesion, ok = _crear_sesion_moodle(usuario or "", contraseña or "", moodle_base)
	if not ok or sesion is None:
		agregar_log("[warning] No se pudo autenticar. Abortando extracción de PDFs.")
		return []

	try:
		respuesta = sesion.get(base_url, timeout=30)
		respuesta.raise_for_status()
		if "login" in respuesta.url.lower():
			agregar_log("[warning] Redirigido al login tras acceder al curso. Sesión inválida.")
			return []
		html = respuesta.text
		agregar_log(f"[debug] HTML descargado con {len(html)} caracteres")
	except Exception as e:
		agregar_log(f"[warning] No se pudo descargar la página: {e}")
		return []

	soup = BeautifulSoup(html, "html.parser")
	enlaces_raw = soup.find_all("a", href=True)
	agregar_log(f"[debug] Enlaces detectados en el HTML: {len(enlaces_raw)}")

	pdfs_directos = []
	recursos_moodle = []
	vistos = set()

	for tag in enlaces_raw:
		href = tag["href"].replace("&amp;", "&").strip()
		absoluta = urljoin(base_url, href)

		if absoluta in vistos:
			continue
		vistos.add(absoluta)

		if _es_url_pdf_directa(absoluta):
			pdfs_directos.append(absoluta)
			agregar_log(f"[info] PDF directo: {absoluta}")
		elif _es_recurso_moodle(absoluta):
			recursos_moodle.append(absoluta)

	# También buscar pluginfile.php embebidos en el HTML
	patron_plugin = re.compile(
		r'https?://[^\s"\'<>]*pluginfile\.php[^\s"\'<>]*\.pdf[^\s"\'<>]*',
		re.IGNORECASE,
	)
	for match in patron_plugin.findall(html):
		enlace = match.replace("&amp;", "&")
		if enlace not in vistos:
			vistos.add(enlace)
			pdfs_directos.append(enlace)
			agregar_log(f"[info] PDF en pluginfile (HTML): {enlace}")

	agregar_log(f"[debug] Recursos Moodle a verificar: {len(recursos_moodle)}")

	# Resolver recursos Moodle para ver si son PDFs
	pdfs_resueltos = []
	for idx, recurso_url in enumerate(recursos_moodle, 1):
		agregar_log(f"[debug] Verificando recurso {idx}/{len(recursos_moodle)}: {recurso_url}")
		pdf_final = _resolver_redireccion_pdf(recurso_url, sesion)
		if pdf_final:
			if pdf_final not in vistos:
				vistos.add(pdf_final)
				pdfs_resueltos.append(pdf_final)
			agregar_log(f"[info] Recurso es PDF: {pdf_final}")
		else:
			agregar_log(f"[warning] No es PDF: {recurso_url}")

	todos_los_pdfs = pdfs_directos + pdfs_resueltos
	agregar_log(f"[info] Total PDFs encontrados: {len(todos_los_pdfs)} "
				f"({len(pdfs_directos)} directos, {len(pdfs_resueltos)} resueltos)")
	return todos_los_pdfs


def obtener_url_actual_browser():
	"""Lee la URL actual del navegador desde la barra de direcciones."""
	try:
		pyautogui.hotkey('ctrl', 'l')
		time.sleep(0.5)
		pyautogui.hotkey('ctrl', 'c')
		time.sleep(0.5)
		resultado = subprocess.run(
			["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="ignore",
		)
		url_actual = resultado.stdout.strip()
		agregar_log(f"[debug] URL actual detectada: {url_actual}")
		return url_actual
	except Exception as e:
		agregar_log(f"[warning] No se pudo leer la URL actual del navegador: {e}")
		return ""


def preguntar_descarga_pdfs(pdf_urls):
	resultado = {"value": False}
	evento = threading.Event()
	prompt_queue.put((pdf_urls, resultado, evento))
	evento.wait()
	return resultado["value"]


def _nombre_archivo_desde_url(pdf_url, indice):
	"""Construye un nombre de archivo razonable para guardar el PDF."""
	parsed = urlparse(pdf_url)
	nombre = os.path.basename(parsed.path)
	if not nombre or not nombre.lower().endswith(".pdf"):
		params = parse_qs(parsed.query)
		cand = params.get("forcedownload") or params.get("file") or params.get("name")
		if cand and cand[0].lower().endswith(".pdf"):
			nombre = cand[0]
		else:
			nombre = f"documento_{indice:03d}.pdf"
	if not nombre.lower().endswith(".pdf"):
		nombre += ".pdf"
	return nombre


def descargar_pdfs(pdf_urls, usuario, contraseña, pagina_url):
	"""Descarga los PDFs detectados y los guarda en output/pdfs."""
	if not pdf_urls:
		agregar_log("[info] No hay PDFs para descargar")
		return 0

	moodle_base = _detectar_moodle_base(pagina_url)
	sesion, ok = _crear_sesion_moodle(usuario or "", contraseña or "", moodle_base)
	if not ok or sesion is None:
		agregar_log("[warning] No se pudo autenticar para descargar los PDFs")
		return 0

	output_dir = os.path.join("output", "pdfs")
	os.makedirs(output_dir, exist_ok=True)
	success = 0

	for i, pdf_url in enumerate(pdf_urls, 1):
		url_limpia = pdf_url.replace("&amp;", "&")
		agregar_log(f"[info] Descargando {i}/{len(pdf_urls)}: {url_limpia}")
		try:
			resp = sesion.get(url_limpia, timeout=60, allow_redirects=True, stream=True)
			resp.raise_for_status()
			content_type = resp.headers.get("Content-Type", "").lower()
			if "pdf" not in content_type and not _es_url_pdf_directa(resp.url):
				agregar_log(f"[warning] Recurso no parece PDF (Content-Type: {content_type})")
				continue

			nombre = _nombre_archivo_desde_url(resp.url, i)
			ruta = os.path.join(output_dir, nombre)
			base, ext = os.path.splitext(ruta)
			n = 1
			while os.path.exists(ruta):
				ruta = f"{base}_{n}{ext}"
				n += 1

			with open(ruta, "wb") as f:
				for chunk in resp.iter_content(chunk_size=8192):
					if chunk:
						f.write(chunk)
			success += 1
			agregar_log(f"[info] Guardado: {ruta}")
		except Exception as e:
			agregar_log(f"[warning] Error descargando {url_limpia}: {e}")

	agregar_log(f"[info] Descargas completadas: {success}/{len(pdf_urls)}")
	return success


def buscar_y_click(imagen_path, threshold=0.85, timeout=6):
	start = time.time()
	captura_path = "captura_temp_search.png"
	plantilla = cv2.imread(imagen_path, cv2.IMREAD_GRAYSCALE)
	if plantilla is None:
		agregar_log(f"[warning] Plantilla no encontrada: {imagen_path}")
		return False
	while time.time() - start < timeout:
		try:
			captura = pyautogui.screenshot()
			captura.save(captura_path)
			img_gray = cv2.cvtColor(cv2.imread(captura_path), cv2.COLOR_BGR2GRAY)
			res = cv2.matchTemplate(img_gray, plantilla, cv2.TM_CCOEFF_NORMED)
			min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
			if max_val >= threshold:
				x, y = max_loc
				centro_x = x + plantilla.shape[1] // 2
				centro_y = y + plantilla.shape[0] // 2
				pyautogui.moveTo(centro_x, centro_y, 0.5)
				pyautogui.click(centro_x, centro_y)
				return True
			time.sleep(0.5)
		except Exception as e:
			agregar_log(f"[warning] Error en búsqueda de imagen {imagen_path}: {e}")
			return False
	return False


def cerrar_aviso_poll():
	poll_path = r"images\\poll.png"
	plantilla = cv2.imread(poll_path, cv2.IMREAD_GRAYSCALE)
	if plantilla is None:
		agregar_log(f"[warning] No se pudo cargar la plantilla del poll: {poll_path}")
		return False

	for intento in range(3):
		agregar_log(f"[debug] Intento {intento + 1}: buscando aviso de encuesta completo...")
		captura = pyautogui.screenshot()
		captura_path = "captura_temp_poll.png"
		captura.save(captura_path)
		img_gray = cv2.cvtColor(cv2.imread(captura_path), cv2.COLOR_BGR2GRAY)
		res = cv2.matchTemplate(img_gray, plantilla, cv2.TM_CCOEFF_NORMED)
		_, max_val, _, max_loc = cv2.minMaxLoc(res)
		if max_val < 0.82:
			agregar_log("[debug] Poll no detectado en este intento")
			return False

		x, y = max_loc
		ancho = plantilla.shape[1]
		alto = plantilla.shape[0]
		agregar_log(f"[info] Poll detectado, intentando cerrar...")
		for offset_x, offset_y in [(25, 25), (35, 25), (30, 30), (40, 20)]:
			pyautogui.click(x + ancho - offset_x, y + offset_y)
			time.sleep(0.8)
			captura = pyautogui.screenshot()
			captura.save(captura_path)
			img_gray = cv2.cvtColor(cv2.imread(captura_path), cv2.COLOR_BGR2GRAY)
			res = cv2.matchTemplate(img_gray, plantilla, cv2.TM_CCOEFF_NORMED)
			_, max_val_after, _, _ = cv2.minMaxLoc(res)
			if max_val_after < 0.82:
				return True
			agregar_log("[warning] El poll sigue visible tras el clic")
		pyautogui.press('esc')
		time.sleep(1)
	return False


def ejecutar_bot():
	try:
		time.sleep(1)
		agregar_log("[debug] Leyendo credenciales desde credentials.txt...")
		cred_path = "credentials.txt"
		usuario = None
		contraseña = None
		try:
			with open(cred_path, "r", encoding="utf-8") as f:
				lines = [l.strip() for l in f.readlines() if l.strip()]
				if len(lines) >= 2:
					usuario = lines[0]
					contraseña = lines[1]
				elif len(lines) == 1:
					usuario = lines[0]
					contraseña = ""
		except Exception as e:
			agregar_log(f"[warning] No se pudo leer {cred_path}: {e}")
			return

		if not usuario:
			agregar_log("[warning] Usuario no encontrado en el archivo de credenciales")
			return

		time.sleep(1)
		url = "https://campusvirtual.ull.es/2526/doctoradoyposgrado"
		agregar_log(f"[info] Abriendo navegador en modo incógnito en {url}...")
		opened = False
		edge_paths = [shutil.which("msedge"),
				r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
				r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"]
		edge_exe = next((p for p in edge_paths if p and os.path.exists(p)), None)
		if edge_exe:
			try:
				subprocess.Popen([edge_exe, "--inprivate", url])
				opened = True
				agregar_log("[info] Edge abierto en modo InPrivate")
			except Exception as e:
				agregar_log(f"[warning] Error abriendo Edge: {e}")
		if not opened:
			chrome_paths = [shutil.which("chrome"), shutil.which("chrome.exe"), shutil.which("google-chrome"),
					r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
					r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"]
			chrome_exe = next((p for p in chrome_paths if p and os.path.exists(p)), None)
			if chrome_exe:
				try:
					subprocess.Popen([chrome_exe, "--incognito", url])
					opened = True
					agregar_log("[info] Chrome abierto en modo incógnito")
				except Exception as e:
					agregar_log(f"[warning] Error abriendo Chrome: {e}")
		if not opened:
			agregar_log("[warning] No se encontró Edge/Chrome. Abriendo navegador por defecto (no incógnito).")
			webbrowser.open(url)
   
		time.sleep(1)
		agregar_log("[debug] Esperando que cargue la página...")
		time.sleep(8)

		agregar_log("[debug] Buscando botón 'aceptar cookies'...")
		if buscar_y_click(r"images\\aceptar_cookies.png", threshold=0.85, timeout=6):
			agregar_log("[info] Aceptar cookies pulsado")
			time.sleep(1)
		else:
			agregar_log("[debug] No apareció ventana de cookies")

		time.sleep(1)
		agregar_log("[debug] Buscando botón de login...")
		if buscar_y_click(r"images\\login_button.png", threshold=0.85, timeout=8):
			agregar_log("[info] Botón login pulsado")
			time.sleep(1)
		else:
			agregar_log("[warning] Botón login no encontrado")

		try:
			time.sleep(1)
			agregar_log(f"[debug] Escribiendo usuario: {usuario}")
			pyautogui.typewrite(usuario)
			time.sleep(1)
			agregar_log("[debug] Moviendo al campo de contraseña con Tab")
			pyautogui.press('tab')
			time.sleep(1)
			agregar_log("[debug] Escribiendo contraseña")
			pyautogui.typewrite(contraseña)
			time.sleep(1)
			agregar_log("[debug] Presionando Enter para iniciar sesión")
			pyautogui.press('enter')
			agregar_log("[info] Credenciales ingresadas correctamente")
			time.sleep(1)
		except Exception:
			agregar_log("[warning] No se completaron las credenciales automáticamente")


		time.sleep(2)
		agregar_log("[debug] Buscando aviso de encuesta...")
		if cerrar_aviso_poll():
			agregar_log("[info] Aviso de encuesta cerrado")
			time.sleep(1)
		else:
			agregar_log("[debug] No apareció aviso de encuesta")

		agregar_log("[debug] Buscando asignatura de informática...")
		informatica_encontrada = buscar_y_click(r"images\\informatica.png", threshold=0.85, timeout=8)
		if not informatica_encontrada:
			agregar_log("[debug] Deslizando hacia abajo para buscar más contenido...")
			ancho, alto = pyautogui.size()
			pyautogui.moveTo(ancho // 2, alto // 2)
			time.sleep(0.5)
			pyautogui.scroll(-50)
			time.sleep(1)
			agregar_log("[debug] Reintentando búsqueda de informática...")
			informatica_encontrada = buscar_y_click(r"images\\informatica.png", threshold=0.85, timeout=8)

		if informatica_encontrada:
			agregar_log("[info] Asignatura de informática abierta")
			# Dar tiempo a que cargue la página del curso
			time.sleep(3)

			agregar_log("[debug] Buscando aviso de encuesta...")
			if cerrar_aviso_poll():
				agregar_log("[info] Aviso de encuesta cerrado")
				time.sleep(1)
			else:
				agregar_log("[debug] No apareció aviso de encuesta")

			agregar_log("[debug] Buscando PDFs dentro de informática...")
			url_informatica = obtener_url_actual_browser() or url
			pdfs_encontrados = extraer_pdfs_desde_pagina(url_informatica, usuario=usuario, contraseña=contraseña)

			if pdfs_encontrados:
				agregar_log(f"[info] Se encontraron {len(pdfs_encontrados)} PDF(s)")
				for pdf in pdfs_encontrados:
					agregar_log(f"[debug] {pdf}")
				if preguntar_descarga_pdfs(pdfs_encontrados):
					agregar_log("[info] El usuario indicó que sí se deben descargar los PDFs")
					descargados = descargar_pdfs(pdfs_encontrados, usuario, contraseña, url_informatica)
					agregar_log(f"[info] PDFs descargados: {descargados}")
				else:
					agregar_log("[info] El usuario indicó que no se descarguen los PDFs")
			else:
				agregar_log("[warning] No se encontraron PDFs dentro de informática")
		else:
			agregar_log("[warning] No se encontró la asignatura de informática")

		time.sleep(3)

	except Exception as e:
		agregar_log(f"[warning] Error: {str(e)}")


process_queue()

thread = threading.Thread(target=ejecutar_bot)
thread.start()

ventana.mainloop()