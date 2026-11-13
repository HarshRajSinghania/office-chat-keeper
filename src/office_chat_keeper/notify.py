"""Notificação do Windows, sem dependências e sem PowerShell.

Como funciona
-------------
O Windows mostra avisos (os "balões"/toasts no canto da tela) em nome de um
ícone na área de notificação. Então, para avisar, o programa:

1. cria uma janela invisível (todo ícone precisa de uma janela "dona");
2. registra um ícone temporário com a mensagem (``Shell_NotifyIconW``);
3. espera alguns segundos, para o aviso ser exibido;
4. remove o ícone e a janela.

Essas funções moram em DLLs do Windows (``user32``, ``shell32``). O módulo
``ctypes`` do Python permite chamá-las diretamente; por isso não precisamos de
nenhuma biblioteca extra.

A notificação é um EXTRA: se o Windows estiver com notificações desligadas ou
em "Não incomodar", ela não aparece. Por isso o alerta também fica no relatório,
no ``status`` e no comando ``alerts``. Nada aqui acessa a rede.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

_NIM_ADD, _NIM_DELETE = 0, 2
_NIF_ICON, _NIF_TIP, _NIF_INFO = 0x02, 0x04, 0x10
_NIIF_INFO, _NIIF_WARNING = 0x01, 0x02
_IDI_WARNING, _IDI_INFORMATION = 32515, 32516
_PM_REMOVE = 0x0001


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    # A ordem e o tamanho dos campos seguem a definição do Windows (shellapi.h).
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID), ("hBalloonIcon", wintypes.HANDLE),
    ]


def notify(title: str, message: str, *, warning: bool = True, seconds: float = 8.0) -> bool:
    """Mostra um aviso do Windows. Devolve True se o Windows aceitou o pedido.

    "Aceitou" não garante que você viu: o Windows pode suprimir avisos.
    Qualquer falha aqui é engolida de propósito - a notificação nunca pode
    derrubar o backup.
    """
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HANDLE, wintypes.HANDLE, wintypes.LPVOID]
        user32.LoadIconW.restype = wintypes.HANDLE
        user32.LoadIconW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]

        # "STATIC" é uma classe de janela pronta do Windows; sem WS_VISIBLE ela não aparece.
        hwnd = user32.CreateWindowExW(0, "STATIC", "office-chat-keeper", 0, 0, 0, 0, 0,
                                      None, None, None, None)
        if not hwnd:
            return False
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = _NIF_ICON | _NIF_TIP | _NIF_INFO
        data.hIcon = user32.LoadIconW(None, _IDI_WARNING if warning else _IDI_INFORMATION)
        data.szTip = "office-chat-keeper"
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = _NIIF_WARNING if warning else _NIIF_INFO
        try:
            if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
                return False
            # Mantém o processo vivo (e atende às mensagens da janela) enquanto o aviso aparece.
            deadline = time.monotonic() + max(0.0, seconds)
            msg = wintypes.MSG()
            while time.monotonic() < deadline:
                while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, _PM_REMOVE):
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.1)
            return True
        finally:
            shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            user32.DestroyWindow(hwnd)
    except Exception:
        return False
