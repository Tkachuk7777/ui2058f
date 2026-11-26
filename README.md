````markdown
<div align="center">

# 📡 Прозрачный радиомост RS485 ↔ LoRa

**Прошивка для LC-EM-1307 (STM32F103 + SX1276/1278, 434 МГц)**

[![MCU](https://img.shields.io/badge/MCU-STM32F103-03234B?logo=stmicroelectronics&logoColor=white)](https://www.st.com/)
[![Radio](https://img.shields.io/badge/Radio-SX1276%20%2F%20SX1278-orange)](https://www.semtech.com/products/wireless-rf/lora-connect/sx1276)
[![Freq](https://img.shields.io/badge/Frequency-434%20MHz-blueviolet)](#-параметры-связи)
[![Toolchain](https://img.shields.io/badge/Toolchain-Keil%20MDK%20%2B%20ARMCLANG%20V6.19-blue)](#-настройка-проекта-в-keil-mdk)
[![Flash](https://img.shields.io/badge/Flash-stm32flash%200.7-green)](#2-прошивка-через-uart-stm32flash)

</div>

---

Универсальная **двусторонняя** прошивка для плат **LC-EM-1307** на базе **STM32F103** и радиомодуля **Semtech SX1276/SX1278 (434 МГц)**. Объединяет два устройства по **RS485** (через `USART2`) в прозрачный беспроводной канал связи — без изменения протокола обмена.

> [!TIP]
> Подходит для **Modbus RTU**, телеметрии и любых бинарных или текстовых пакетов до 128 байт.

---

## Содержание

- [Структура и пути проекта](#-структура-и-пути-проекта)
- [Настройка проекта в Keil MDK](#-настройка-проекта-в-keil-mdk)
- [Сборка и прошивка](#-сборка-и-прошивка)
- [Основные изменения в прошивке](#-основные-изменения-в-прошивке-mainc)
- [Распиновка](#-распиновка-lc-em-1307--stm32f103)
- [Параметры связи](#-параметры-связи)
- [Индикация LED](#-индикация-led-pb12)
- [Скрипт проверки моста (Python)](#-скрипт-двусторонней-проверки-моста-python)
- [Быстрый чек-лист](#-быстрый-чек-лист-проверки)

---

## 📁 Структура и пути проекта

| Назначение | Путь |
| :--- | :--- |
| **Файл проекта Keil** | `C:\Temp\LC_EM_1307\LC-EM-1307\project\lora_Debug _RX_TX\LoRaDriverSDK\project\sx127xDriverSDK.uvprojx` |
| **Выходной `.hex`** | `C:\Temp\LC_EM_1307\LC-EM-1307\project\lora_Debug _RX_TX\LoRaDriverSDK\project\Objects\sx127xDriverSDK.hex` |
| **Утилита `stm32flash`** | `C:\tem\stm32flash-0.7-binaries\stm32flash.exe` |

---

## ⚙️ Настройка проекта в Keil MDK

Компиляция под **Arm Compiler 6 (ARMCLANG V6.19)**. Установлены следующие параметры:

| Вкладка | Параметр | Значение |
| :--- | :--- | :--- |
| **Target** | Xtal (MHz) | `8.0` |
| **Target** | ARM Compiler | `Use default compiler version 6` |
| **Target** | Code Generation | ✅ `Use MicroLIB` |
| **Target** | IROM1 (Start / Size) | `0x8000000` / `0x10000` |
| **Output** | Create HEX File | ✅ Включено |
| **C/C++** | Preprocessor Symbols | `USE_STDPERIPH_DRIVER` |

> [!IMPORTANT]
> В проект обязательно подключён модуль `stm32f10x_iwdg.c` (и заголовок `stm32f10x_iwdg.h`) — он нужен для работы сторожевого таймера (IWDG).

---

## 🛠 Сборка и прошивка

### 1. Сборка через Keil UV4

```bat
"C:\Keil_v5\UV4\UV4.exe" -b "C:\Temp\LC_EM_1307\LC-EM-1307\project\lora_Debug _RX_TX\LoRaDriverSDK\project\sx127xDriverSDK.uvprojx" -j0 -o build_log.txt
```

### 2. Прошивка через UART (`stm32flash`)

Перед запуском:

1. Установите **BOOT0 = 1** (высокий уровень).
2. Нажмите **RESET**.
3. Выполните команду:

```bat
C:\tem\stm32flash-0.7-binaries\stm32flash.exe -w "C:\Temp\LC_EM_1307\LC-EM-1307\project\lora_Debug _RX_TX\LoRaDriverSDK\project\Objects\sx127xDriverSDK.hex" -v -g 0x0 COM5
```

| Ключ | Описание |
| :--- | :--- |
| `-w` | Запись HEX-файла во Flash |
| `-v` | Верификация после записи |
| `-g 0x0` | Автозапуск с адреса `0x08000000` |
| `COM5` | Порт USB-UART (например, `COM5` или `COM7`) |

> После сообщения `Starting execution at address 0x08000000... done.` верните **BOOT0 = 0**.

---

## 💻 Основные изменения в прошивке (`main.c`)

### Прямое управление SX127x по SPI

Блокирующие функции SDK удалены. Передача и приём контролируются через `RegIrqFlags` (`0x12`):

| Флаг | HEX | Что означает |
| :--- | :---: | :--- |
| `TxDone` | `0x08` | Ожидание с таймаутом 1000 мс |
| `RxDone` | `0x40` | Чтение пакета с проверкой `PayloadCrcError` |
| `PayloadCrcError` | `0x20` | Ошибка CRC входящего пакета |

### Атомарный приём по USART2 (RS485)

- Кольцевой буфер `rx_buf2[128]`.
- Подтяжка линии RX к питанию (`GPIO_Mode_IPU`) — защита от наводок.
- Корректная очистка `USART_FLAG_ORE` без потери байта.
- Критическая секция (`__disable_irq()` / `__enable_irq()`) при копировании.
- Таймаут кадра: `FRAME_GAP_MS = 50` мс.

### Точное управление RS485 (DE / RE) через DWT

- Микросекундные задержки `delay_us(10)` на базе `DWT_CYCCNT`.
- Переключение `PB13`, `PA8` с выдержкой **10 мкс** до и после передачи.

### Индикация и watchdog

- Неблокирующее мигание LED (`PB12`) через `GetTick()`.
- **IWDG (~2 сек)** включается только после успешной инициализации LoRa.

---

## 🔌 Распиновка (LC-EM-1307 / STM32F103)

| Назначение | Вывод STM32 | Примечание |
| :--- | :---: | :--- |
| USART1 TX / RX | `PA9` / `PA10` | Системный UART (прошивка) |
| USART2 TX / RX | `PA2` / `PA3` | Рабочий канал RS485 (RX с IPU) |
| RS485 DE / RE | `PB13`, `PA8` | `0` — приём, `1` — передача |
| SPI1 (SX127x) | `PA4` (NSS), `PA5` (SCK), `PA6` (MISO), `PA7` (MOSI) | Аппаратный SPI1 |
| SX127x RESET | `PB10` | Активный `0` |
| LED | `PB12` | Статусный светодиод |

---

## 📡 Параметры связи

| Параметр | Значение |
| :--- | :--- |
| **RS485 (USART2)** | `9600` бод, `8N1`, макс. `128` байт, таймаут `50` мс |
| **LoRa частота** | `434.0` МГц (`0x6C8000`) |
| **LoRa BW** | `125` кГц (`RegModemConfig1 = 0x72`) |
| **LoRa SF / CR** | `SF7`, `4/5`, CRC On (`RegModemConfig2 = 0x74`) |
| **LoRa AGC** | AGC Auto On (`RegModemConfig3 = 0x04`) |
| **LoRa мощность** | `+17` дБм, PA_BOOST (`RegPaConfig = 0x8F`) |
| **LoRa SyncWord** | `0x12` |

---

## 💡 Индикация LED (`PB12`)

| Режим | Значение |
| :--- | :--- |
| **Частое мигание (100 мс)** | Ошибка SPI (не пройден `RegVersion == 0x12`) |
| **2 вспышки при старте** | LoRa инициализирован, отправлено `BRIDGE_READY\r\n` |
| **Свечение при передаче** | Отправка кадра в эфир |
| **Вспышка 100 мс** | Успешный приём пакета и выдача в RS485 |

---

## 🐍 Скрипт двусторонней проверки моста (Python)

**Требования:** `pip install pyserial`

```python
import serial
import time
import sys

PORT_A = "COM7"  # Плата 1 (USB-RS485)
PORT_B = "COM5"  # Плата 2 (USB-RS485)
BAUD = 9600
LORA_WAIT = 1.5  # Увеличено для LoRa (было 0.5)

def open_port(port):
    try:
        s = serial.Serial(
            port=port, baudrate=BAUD, timeout=0.5,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE
        )
        s.dtr = False
        s.rts = False
        s.reset_input_buffer()
        s.reset_output_buffer()
        print(f"✅ {port} открыт | Настройки: {s.baudrate} 8N1 | CTS:{s.cts} DSR:{s.dsr}")
        return s
    except Exception as e:
        print(f"❌ Ошибка открытия {port}: {e}")
        return None

def diagnose_silence(sender, receiver, wait_time):
    print(f"    🔍 ДИАГНОСТИКА тишины на {receiver.port}:")
    print(f"       1. Статус портов: {sender.port}={'OPEN' if sender.is_open else 'CLOSED'}, {receiver.port}={'OPEN' if receiver.is_open else 'CLOSED'}")
    print(f"       2. Очередь на отправку в {sender.port}: {sender.out_waiting} байт (если >0, драйвер/RS485 завис)")
    print(f"       3. Линии {receiver.port}: CTS={receiver.cts}, DSR={receiver.dsr}, CD={receiver.cd}")
    print(f"       4. Ждем еще 1.5 сек (проверка задержки эфира LoRa)...")
    time.sleep(1.5)
    if receiver.in_waiting > 0:
        late_data = receiver.read(receiver.in_waiting)
        print(f"       ⚠️ НАЙДЕНО! Пакет пришел с опозданием (> {wait_time}с): {late_data}")
        print(f"       💡 Решение: увеличьте LORA_WAIT в начале скрипта!")
    else:
        print(f"       ❌ Пусто даже спустя {wait_time + 1.5} сек.")
        print(f"       💡 Проверьте: линии A/B (не перепутаны ли?), питание LoRa-модулей, одинаковые ли частота/канал/адрес в настройках LoRa.")

s_a = open_port(PORT_A)
s_b = open_port(PORT_B)

if not s_a or not s_b:
    print("Нужны оба порта. Проверь подключение.")
    sys.exit(1)

print("\n🚀 Тест моста: COM7 <-> COM5 (через LoRa)")
print(f"Таймаут ожидания радиоканала: {LORA_WAIT} сек.\n")

stats = {"A_to_B": 0, "B_to_A": 0, "total": 10}

try:
    for i in range(1, stats["total"] + 1):
        # 1. COM7 -> COM5
        s_b.reset_input_buffer()
        msg = f"FROM_COM7_{i}\n"
        print(f"[{i}] 📤 Отправка в {PORT_A}: {msg.strip()}")
        written = s_a.write(msg.encode())
        s_a.flush()
        print(f"    (В буфер {PORT_A} записано {written} байт)")
        time.sleep(LORA_WAIT)

        if s_b.in_waiting > 0:
            data = s_b.read(s_b.in_waiting)
            print(f"    📥 Принято в {PORT_B} ({len(data)} байт): {data}")
            stats["A_to_B"] += 1
        else:
            print(f"    ❌ Тишина в {PORT_B}")
            diagnose_silence(s_a, s_b, LORA_WAIT)

        # 2. COM5 -> COM7
        s_a.reset_input_buffer()
        msg2 = f"FROM_COM5_{i}\n"
        print(f"[{i}] 📤 Отправка в {PORT_B}: {msg2.strip()}")
        written2 = s_b.write(msg2.encode())
        s_b.flush()
        print(f"    (В буфер {PORT_B} записано {written2} байт)")
        time.sleep(LORA_WAIT)

        if s_a.in_waiting > 0:
            data = s_a.read(s_a.in_waiting)
            print(f"    📥 Принято в {PORT_A} ({len(data)} байт): {data}")
            stats["B_to_A"] += 1
        else:
            print(f"    ❌ Тишина в {PORT_A}")
            diagnose_silence(s_b, s_a, LORA_WAIT)

        print("-" * 50)
        time.sleep(0.5)

except KeyboardInterrupt:
    print("\n👋 Остановлено пользователем.")
finally:
    print("\n📊 ИТОГОВАЯ ДИАГНОСТИКА:")
    print(f"   Успешно {PORT_A} -> {PORT_B}: {stats['A_to_B']} из {stats['total']}")
    print(f"   Успешно {PORT_B} -> {PORT_A}: {stats['B_to_A']} из {stats['total']}")
    s_a.close()
    s_b.close()
```

---

## ✅ Быстрый чек-лист проверки

- [ ] `BOOT0 = 0` после прошивки.
- [ ] Питание 5 V на обеих платах.
- [ ] Антенны прикручены.
- [ ] Линии A/B **не перепутаны** (A → A, B → B).
- [ ] Скорость `9600` в Python-скрипте.
- [ ] `LORA_WAIT ≥ 1.5` сек.
- [ ] Светодиод **2 вспышки** при старте (успех инициализации).
- [ ] Светодиод **частое мигание** — ошибка SPI (проверь `PA4–PA7`, `PB10`).

---

<div align="center">
<sub>Прозрачный радиомост RS485 ↔ LoRa · LC-EM-1307 / STM32F103 + SX127x</sub>
</div>
````

---

## Что поправил относительно исходника

| Проблема | Было | Стало |
|---|---|---|
| **Незакрытый код-блок** | Первый `bat`-блок без закрывающего ``` — «съедал» весь раздел «Прошивка» до самого Python | Закрыт, разбит на два отдельных блока (сборка + прошивка) |
| **Слипшиеся разделы** | `Ключ Описание...` шло сразу за кодом без заголовка | Явный подраздел «Прошивка» + таблица ключей отдельно |
| **Списки без воздуха** | «1. Установите BOOT0… 2. Нажмите RESET…» — слипшиеся строки | Нумерованный список с пустыми строками |
| **Нет шапки / бейджей** | Начиналось с `# 📡` | Центрированный `<div>` + 5 бейджей (MCU, Radio, Freq, Toolchain, Flash) |
| **Нет TOC** | — | Оглавление со ссылками на все секции |
| **Разделители** | Отсутствовали | `---` между всеми крупными секциями |
| **Мета-комментарий в конце** | Был блок «Что заложено в README» (для тебя) | Убран — футер с `<div align="center"><sub>…</sub></div>` |
| **Флаги IRQ вразброс** | Текст `TxDone (0x08) — ожидание…` | Отдельная таблица «Флаг / HEX / Что означает» |
| **Чек-лист без чекбоксов** | ` BOOT0 = 0 после прошивки` (просто отступ) | `- [ ]` — GitHub рендерит как интерактивные чекбоксы |
| **`⚠️` вместо alert** | Текстовые `> Важно:` | `> [!WARNING]` / `> [!TIP]` / `> [!IMPORTANT]` — цветные плашки GitHub |
| **Триколонные таблицы** | `Параметр | Значение` иногда без выравнивания | Все с `:---` / `:---:` / `:---` |
| **Python-скрипт** | Код внутри `python`-блока, но под ним текст шёл сразу (нет закрывающего тэга?) | Явно закрыт, после него отдельный раздел |

## Как применить

```bash
cd ~/python/scripts/UI2058F/metr
# открой README.md и замени содержимое целиком
git add README.md
git commit -m "docs: prettify README (TOC, alerts, pinout table, checklist)"
git push
```
