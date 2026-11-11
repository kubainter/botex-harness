# Konfiguracja powiadomień Telegram dla BoteX i kuratora grafu

Instrukcja krok po kroku konfiguracji powiadomień w Telegramie dla BoteX oraz procesu Knowledge Graph Curator.

## Ochrona plików środowiskowych (Zero .env Access)

Nie wklejaj zawartości pliku `.env` na czat i nie zlecaj agentom AI jego odczytywania. Wszelkie tokeny wprowadzaj ręcznie w edytorze lub bezpośrednio na serwerze.

## Krok 1: Utworzenie bota w Telegramie (@BotFather)

1. Wyszukaj w Telegramie bota `@BotFather` z niebieskim znaczkiem weryfikacji.
2. Rozpocznij rozmowę komendą `/start`.
3. Wyślij polecenie:
   ```text
   /newbot
   ```
4. Podaj nazwę wyświetlaną (np. `BoteX Ops Notifier`) oraz unikalny username zakończony na `bot` (np. `my_botex_alert_bot`).
5. BotFather odeśle token w formacie:
   ```text
   7123456789:AAFlkB4x99_abcdef1234567890XYZ-ABC
   ```
   Zapisz go do zmiennej `TELEGRAM_BOT_TOKEN`.

## Krok 2: Pobranie TELEGRAM_CHAT_ID

Telegram wymaga identyfikatora odbiorcy przed wysłaniem pierwszej wiadomości.

### Czat prywatny (użytkownik z botem)
1. Wyszukaj utworzonego bota w Telegramie i wyślij mu `/start` lub dowolny tekst. Bot nie może zacząć rozmowy sam.
2. Sprawdź identyfikator:
   - w przeglądarce pod adresem:
     ```text
     https://api.telegram.org/bot<TWÓJ_TELEGRAM_BOT_TOKEN>/getUpdates
     ```
     W polu `"chat":{"id": 123456789, ...}` odczytaj wartość `123456789`.
   - lub wyślij `/start` do bota `@userinfobot`, który od razu zwróci Twój identyfikator.

### Grupa lub kanał
1. Utwórz grupę w Telegramie i dodaj do niej bota z uprawnieniem wysyłania wiadomości.
2. Wyślij dowolny tekst na grupie (np. `test`).
3. Otwórz `https://api.telegram.org/bot<TWÓJ_TELEGRAM_BOT_TOKEN>/getUpdates`. Identyfikator grupy to liczba ujemna (np. `-1001234567890`).

## Krok 3: Zmienne konfiguracyjne

| Zmienna | Typ | Domyślnie | Opis |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | String | *(brak)* | Token API bota z `@BotFather`. |
| `TELEGRAM_CHAT_ID` | String/Int | *(brak)* | Identyfikator czatu lub grupy. |
| `TELEGRAM_ALERT_ON_SUCCESS` | Boolean | `false` | Gdy `false`, wysyła alerty tylko przy błędach, awariach lub wykrytych martwych odnośnikach. Gdy `true`, raportuje każdy przebieg. |
| `TELEGRAM_SILENT` | Boolean | `false` | Wysyłanie powiadomień bez dźwięku i wibracji. |

## Krok 4: Wprowadzenie konfiguracji

### Środowisko lokalne
Uzupełnij plik `.env` w głównym katalogu projektu:
```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ALERT_ON_SUCCESS=true
TELEGRAM_SILENT=false
```

### Serwer produkcyjny (VPS Linux)
1. Połącz się przez SSH z serwerem:
   ```bash
   ssh <vps-user>@<adres-ip-serwera>
   ```
2. Otwórz plik środowiskowy:
   ```bash
   sudo nano /opt/botex/.env
   ```
3. Dopisz zmienne `TELEGRAM_*` na końcu pliku.
4. Ustaw uprawnienia tylko do odczytu dla właściciela procesu:
   ```bash
   sudo chmod 600 /opt/botex/.env
   sudo chown botex:botex /opt/botex/.env
   ```
5. Zrestartuj usługę:
   ```bash
   sudo systemctl restart botex-cloud.service
   ```

## Krok 5: Weryfikacja

### Test curl
```bash
curl -s -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/sendMessage" \
  -d "chat_id=<TELEGRAM_CHAT_ID>" \
  -d "text=Test powiadomień BoteX: połączenie aktywne."
```

### Uruchomienie przez REST API
```bash
curl -X POST "http://127.0.0.1:8000/api/curator/run" \
  -H "Content-Type: application/json" \
  -d '{"force": true, "notify_telegram": true}'
```
Kurator przejrzy graf pamięci, zaktualizuje notatkę `curator_health_status.md` i wyśle raport na Telegram.

### Uruchomienie przez CLI
```bash
python -m ops.curator.curator_service --force --notify
```
Lub na serwerze:
```bash
sudo -u botex /usr/local/sbin/botex-curator.sh
```
