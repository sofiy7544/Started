"""AI Model Router: какая модель (или не-модель) делает какую часть работы.

| Задача | Чем | Почему |
|---|---|---|
| текст PDF/DOCX/XLSX/PPTX/CSV/JSON/XML | локальный разбор (documents.py) | бесплатно и точно |
| речь (голосовые, аудио, звук видео) | STT Voice Engine (gpt-4o-mini-transcribe или faster-whisper) | специализированная, ≈$0.003/мин |
| озвучка ответа | TTS Voice Engine | специализированная |
| фото, скриншоты, сканы, кадры видео | выбранная модель, если видит изображения, иначе `vision` из каталога | нужна мультимодальность |
| конспект большого документа по частям | дешёвая модель (`memory` в каталоге), результат кэшируется | много текста — дёшево |
| ответ владельцу | выбранная модель | качество ответа |
"""

from __future__ import annotations

from pai.config import Settings
from pai.llm.catalog import Catalog, ModelInfo


class ModelRouter:
    def __init__(self, catalog: Catalog, settings: Settings) -> None:
        self._catalog = catalog
        self._settings = settings

    def for_answer(self, chosen: ModelInfo, *, needs_vision: bool) -> ModelInfo | None:
        """Модель ответа. None — нужны изображения, но ни одна доступная модель их не видит."""
        if not needs_vision or chosen.vision:
            return chosen
        return self._catalog.vision_model(self._settings)

    def for_digest(self) -> ModelInfo:
        """Дешёвая модель для конспекта частей большого документа."""
        return self._catalog.memory_model(self._settings)
