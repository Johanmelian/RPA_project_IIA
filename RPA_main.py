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
from urllib.parse import urljoin
from tkinter import messagebox
import requests
from bs4 import BeautifulSoup
import importlib

# Crear ventana de logs
ventana = tk.Tk()
ventana.title("Logs del Bot")
ventana.geometry("500x300+0+0")  # Posicionar en esquina superior izquierda
ventana.attributes("-topmost", True)

log_text = scrolledtext.ScrolledText(ventana, wrap=tk.WORD, bg="#f0f0f0", font=("Arial", 9))
log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
log_text.config(state=tk.DISABLED)

log_queue = queue.Queue()
prompt_queue = queue.Queue()

def agregar_log(mensaje):
	"""Encola un mensaje de log para que el hilo principal lo procese."""
	log_queue.put(mensaje)


def process_queue():
	"""Procesa la cola de logs en el hilo principal y actualiza el widget."""
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


def extraer_pdfs_desde_pagina(base_url):
	"""Obtiene la página con cookies del navegador y extrae enlaces PDF."""
	agregar_log("🔎 Obteniendo la página con cookies del navegador...")
	try:
		browser_cookie3 = importlib.import_module("browser_cookie3")
		cookies = browser_cookie3.edge(domain_name="campusvirtual.ull.es")
	except Exception:
		try:
			browser_cookie3 = importlib.import_module("browser_cookie3")
			cookies = browser_cookie3.chrome(domain_name="campusvirtual.ull.es")
		except Exception as e:
			agregar_log(f"❌ No se pudieron leer cookies del navegador: {e}")
			return []

	sesion = requests.Session()
	sesion.cookies.update(cookies)
	try:
		respuesta = sesion.get(base_url, timeout=30)
		respuesta.raise_for_status()
		html = respuesta.text
		agregar_log(f"📄 HTML descargado con {len(html)} caracteres")
	except Exception as e:
		agregar_log(f"❌ No se pudo descargar la página: {e}")
		return []

	soup = BeautifulSoup(html, "html.parser")
	pdfs = []
	for enlace in soup.find_all("a", href=True):
		href = enlace["href"].replace('&amp;', '&')
		if ".pdf" in href.lower():
			enlace_absoluto = urljoin(base_url, href)
			if enlace_absoluto not in pdfs:
				pdfs.append(enlace_absoluto)

	if not pdfs:
		patron = re.compile(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*|[^\s"\'<>]+\.pdf[^\s"\'<>]*', re.IGNORECASE)
		coincidencias = patron.findall(html)
		agregar_log(f"📎 Coincidencias PDF encontradas en el HTML: {len(coincidencias)}")
		for enlace in coincidencias:
			enlace = enlace.replace('&amp;', '&')
			enlace_absoluto = urljoin(base_url, enlace)
			if enlace_absoluto not in pdfs:
				pdfs.append(enlace_absoluto)

	agregar_log(f"📎 PDFs deducidos tras parsear HTML: {len(pdfs)}")
	return pdfs


def preguntar_descarga_pdfs(pdf_urls):
	"""Solicita en la interfaz principal si se deben descargar los PDFs encontrados."""
	resultado = {"value": False}
	evento = threading.Event()
	prompt_queue.put((pdf_urls, resultado, evento))
	evento.wait()
	return resultado["value"]


def buscar_y_click(imagen_path, threshold=0.85, timeout=6):
	"""Busca una imagen en pantalla y hace clic en su centro si la encuentra.
	Devuelve True si la encontró y clicó, False en caso contrario.
	"""
	start = time.time()
	captura_path = "captura_temp_search.png"
	plantilla = cv2.imread(imagen_path, cv2.IMREAD_GRAYSCALE)
	if plantilla is None:
		agregar_log(f"❌ Plantilla no encontrada: {imagen_path}")
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
			agregar_log(f"⚠️ Error en búsqueda de imagen {imagen_path}: {e}")
			return False
	return False


def cerrar_aviso_poll():
	"""Intenta cerrar el aviso de encuesta y verifica que desaparezca."""
	poll_path = r"images\\poll.png"
	plantilla = cv2.imread(poll_path, cv2.IMREAD_GRAYSCALE)
	if plantilla is None:
		agregar_log(f"❌ No se pudo cargar la plantilla del poll: {poll_path}")
		return False

	for intento in range(3):
		agregar_log(f"🔎 Intento {intento + 1}: buscando aviso de encuesta completo...")
		captura = pyautogui.screenshot()
		captura_path = "captura_temp_poll.png"
		captura.save(captura_path)
		img_gray = cv2.cvtColor(cv2.imread(captura_path), cv2.COLOR_BGR2GRAY)
		res = cv2.matchTemplate(img_gray, plantilla, cv2.TM_CCOEFF_NORMED)
		_, max_val, _, max_loc = cv2.minMaxLoc(res)
		if max_val < 0.82:
			agregar_log("— Poll no detectado en este intento")
			return False

		x, y = max_loc
		ancho = plantilla.shape[1]
		alto = plantilla.shape[0]
		agregar_log(f"✅ Poll detectado, intentando cerrar...")
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
			agregar_log("⚠️ El poll sigue visible tras el clic")
		pyautogui.press('esc')
		time.sleep(1)
	return False

def ejecutar_bot():
	try:
		# Leer credenciales desde archivo
		agregar_log("🔐 Leyendo credenciales desde credentials.txt...")
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
			agregar_log(f"❌ No se pudo leer {cred_path}: {e}")
			return
		
		if not usuario:
			agregar_log("❌ Usuario no encontrado en el archivo de credenciales")
			return
		
		url = "https://campusvirtual.ull.es/2526/doctoradoyposgrado"
		agregar_log(f"🌐 Abriendo navegador en modo incógnito en {url}...")
		opened = False
		# Intentar Edge (InPrivate) buscando en PATH y rutas comunes
		edge_paths = [shutil.which("msedge"),
				r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
				r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"]
		edge_exe = next((p for p in edge_paths if p and os.path.exists(p)), None)
		if edge_exe:
			try:
				subprocess.Popen([edge_exe, "--inprivate", url])
				opened = True
				agregar_log("🌐 Edge abierto en modo InPrivate")
			except Exception as e:
				agregar_log(f"⚠️ Error abriendo Edge: {e}")
		# Intentar Chrome (incognito)
		if not opened:
			# Intentar Chrome buscando en PATH y rutas comunes
			chrome_paths = [shutil.which("chrome"), shutil.which("chrome.exe"), shutil.which("google-chrome"),
					r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
					r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"]
			chrome_exe = next((p for p in chrome_paths if p and os.path.exists(p)), None)
			if chrome_exe:
				try:
					subprocess.Popen([chrome_exe, "--incognito", url])
					opened = True
					agregar_log("🌐 Chrome abierto en modo incógnito")
				except Exception as e:
					agregar_log(f"⚠️ Error abriendo Chrome: {e}")
		# Fallback: navegador por defecto (no incógnito)
		if not opened:
			agregar_log("⚠️ No se encontró Edge/Chrome. Abriendo navegador por defecto (no incógnito).")
			webbrowser.open(url)
		
		agregar_log("⏳ Esperando que cargue la página...")
		time.sleep(8)
		# Comprobar ventana de cookies y rechazarla si aparece
		agregar_log("🔎 Buscando botón 'rechazar cookies'...")
		if buscar_y_click(r"images\\rechazar_cookies.png", threshold=0.85, timeout=6):
			agregar_log("✅ Rechazar cookies pulsado")
			time.sleep(1)
		else:
			agregar_log("— No apareció ventana de cookies")
			time.sleep(1)
		
		# Entrar en login
		agregar_log("🔎 Buscando botón de login...")
		if buscar_y_click(r"images\\login_button.png", threshold=0.85, timeout=8):
			agregar_log("✅ Botón login pulsado")
			time.sleep(1)
		else:
			agregar_log("❌ Botón login no encontrado")
			time.sleep(1)
		
		# Ingresar credenciales
		try:
			time.sleep(1)
			agregar_log(f"✏️ Escribiendo usuario: {usuario}")
			pyautogui.typewrite(usuario)
			time.sleep(1)
			agregar_log("⏫ Moviendo al campo de contraseña con Tab")
			pyautogui.press('tab')
			time.sleep(1)
			agregar_log("✏️ Escribiendo contraseña")
			pyautogui.typewrite(contraseña)
			time.sleep(1)
			agregar_log("🔓 Presionando Enter para iniciar sesión")
			pyautogui.press('enter')
			agregar_log("✓ Credenciales ingresadas correctamente")
			time.sleep(1)
		except Exception:
			agregar_log("⚠️ No se completaron las credenciales automáticamente")
		
		agregar_log("🔎 Buscando aviso de encuesta...")
		time.sleep(1)
		if cerrar_aviso_poll():
			agregar_log("✅ Aviso de encuesta cerrado")
			time.sleep(1)
		else:
			agregar_log("— No apareció aviso de encuesta")
			time.sleep(1)
		
		agregar_log("🔎 Buscando asignatura de informática...")
		informatica_encontrada = buscar_y_click(r"images\\informatica.png", threshold=0.85, timeout=8)
		if not informatica_encontrada:
			agregar_log("📜 Deslizando hacia abajo para buscar más contenido...")
			ancho, alto = pyautogui.size()
			pyautogui.moveTo(ancho // 2, alto // 2)  # Mover cursor al centro de la pantalla
			time.sleep(0.5)
			pyautogui.scroll(-50)  # Scroll
			time.sleep(1)
			agregar_log("🔎 Reintentando búsqueda de informática...")
			informatica_encontrada = buscar_y_click(r"images\\informatica.png", threshold=0.85, timeout=8)
		
		if informatica_encontrada:
			agregar_log("✅ Asignatura de informática abierta")
			time.sleep(1)
   
			agregar_log("🔎 Buscando aviso de encuesta...")
			time.sleep(1)
			if cerrar_aviso_poll():
				agregar_log("✅ Aviso de encuesta cerrado")
				time.sleep(1)
			else:
				agregar_log("— No apareció aviso de encuesta")
				time.sleep(1) 
    
			agregar_log("🔎 Buscando PDFs dentro de informática...")
			pdfs_encontrados = extraer_pdfs_desde_pagina(url)
			if pdfs_encontrados:
				agregar_log(f"✅ Se encontraron {len(pdfs_encontrados)} PDF(s)")
				for pdf in pdfs_encontrados:
					agregar_log(f"📄 {pdf}")
				if preguntar_descarga_pdfs(pdfs_encontrados):
					agregar_log("✅ El usuario indicó que sí se deben descargar los PDFs")
				else:
					agregar_log("ℹ️ El usuario indicó que no se descarguen los PDFs")
			else:
				agregar_log("— No se encontraron PDFs dentro de informática")
		else:
			agregar_log("❌ No se encontró la asignatura de informática")
			time.sleep(1)
   

  
		time.sleep(3)
		
	except Exception as e:
		agregar_log(f"⚠️ Error: {str(e)}")

# Iniciar procesamiento de la cola de logs en el hilo principal
process_queue()

# Ejecutar en thread separado
thread = threading.Thread(target=ejecutar_bot)
thread.start()

ventana.mainloop()