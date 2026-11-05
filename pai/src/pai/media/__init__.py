"""Multimodal Processing Engine: фото, документы, аудио, видео, архивы → понятный модели контекст.

Модули:
  files.py      — Telegram File Handler: что пришло, лимиты, скачивание во временный файл
  safety.py     — проверка типа по содержимому, запрет исполняемых файлов, защита от zip-бомб
  images.py     — Image Understanding: подготовка изображений (размер, EXIF, формат)
  ocr.py        — OCR: страницы без текстового слоя → изображение для мультимодальной модели
  documents.py  — Document Parser: PDF, DOCX, XLSX, CSV, TXT, PPTX, JSON, XML, ZIP
  video.py      — Video Processor: кадры по времени и звуковая дорожка (PyAV)
  store.py      — хранение вложений: база (текст по частям) + временные файлы на диске
  context.py    — Context Manager: вложения разговора, что показать модели сейчас
  router.py     — AI Model Router: какая модель для какой задачи
  tools.py      — инструменты модели: посмотреть, прочитать часть, найти, конспект, кадры, файл
  outputs.py    — создание новых файлов (txt, md, csv, json, xlsx, docx)
  engine.py     — сборка: обработка входящего файла целиком
Распознавание и синтез речи — существующий Voice Engine (pai.voice).
"""
