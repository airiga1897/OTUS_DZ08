# ДЗ08. Очередь журналов Kafka

## Задание

Собрать журналы Nginx и WordPress, передать их через Kafka в Elasticsearch
и показать в Kibana. Для каждого источника нужен свой топик с двумя партициями
и двумя репликами, отдельный индекс и представление данных.

## Решение

| ВМ в Yandex Cloud | Что работает |
| --- | --- |
| **portal** | Nginx, WordPress и Filebeat. 2 vCPU, 4 ГБ RAM, диск 20 ГБ. |
| **logs** | Kafka, Logstash, Elasticsearch, Kibana и Filebeat. 2 vCPU, 12 ГБ RAM, диск 20 ГБ. |

Filebeat на портале отправляет журналы в топики `nginx` и `wordpress`. Logstash
читает эти топики и записывает события в индексы `logs-nginx` и `logs-wordpress`.
Для каждого индекса в Kibana настроено представление данных с полем `@timestamp`.

Два брокера Kafka на одной ВМ позволяют создать по две реплики в каждой
партиции. При отключении ВМ **logs** оба брокера и ELK становятся недоступны.

## Запуск

На Windows выполняйте команды из корня клона в PowerShell 5.1 или 7. Нужны
Python 3.12–3.14, OpenSSH, Terraform от 1.6 и Yandex Cloud CLI. Оператору
нужны права на управление Compute Cloud и VPC в каталоге и просмотр квот облака.

Если профиль ещё не настроен, выполните `yc init --no-browser` и выберите облако
и каталог стенда. Для локальной копии CLI используйте
`.\.tools\bin\yc.exe init --no-browser`. Подготовьте окружение и получите
временный токен из активного профиля в той же сессии PowerShell.

```powershell
.\scripts\setup.ps1
if ($LASTEXITCODE -ne 0) { throw 'Подготовка окружения не завершилась' }

$yc = (Get-Command yc -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $yc) { $yc = (Resolve-Path .\.tools\bin\yc.exe -ErrorAction Stop).Path }
$env:YC_CLOUD_ID = & $yc config get cloud-id
if ($LASTEXITCODE -ne 0 -or -not $env:YC_CLOUD_ID) { throw 'Не получен ID облака YC' }
$env:YC_FOLDER_ID = & $yc config get folder-id
if ($LASTEXITCODE -ne 0 -or -not $env:YC_FOLDER_ID) { throw 'Не получен ID каталога YC' }
$env:YC_TOKEN = & $yc iam create-token --no-browser
if ($LASTEXITCODE -ne 0 -or -not $env:YC_TOKEN) { throw 'Не получен IAM-токен YC' }
```

`deploy` проверяет квоты перед созданием стенда. Если SSH может идти через
несколько шлюзов, до запуска перечислите их подтверждённые публичные адреса
`/32` в массиве `admin_cidrs` локального файла `config/lab.local.json`.
После успешного развёртывания отдельно загрузите восемь материалов WordPress.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py deploy
if ($LASTEXITCODE -ne 0) { throw 'Развёртывание не завершилось' }
.\.venv\Scripts\python.exe scripts/lab.py import-content
```

## Проверка

```powershell
.\.venv\Scripts\python.exe scripts/lab.py verify
.\.venv\Scripts\python.exe scripts/lab.py open
```

`verify` проверяет параметры топиков и прохождение событий обоих источников
через Kafka и Logstash в Elasticsearch. `open` держит открытыми SSH-туннели
к [WordPress](http://localhost:8080) и [Kibana](http://localhost:5601).
В Kibana войдите как `elastic` с паролем из поля `elastic_password` файла
`.local/secrets.json`. В **Discover** выберите последние 15 минут и по очереди
откройте **ДЗ08 nginx** и **ДЗ08 wordpress**. Контрольные события находятся
по запросу `otus.marker: *`.

В облачном прогоне подтверждены две партиции и две синхронные реплики в каждом
топике. События контрольного запроса найдены в обоих индексах через API Kibana.
После проверки ресурсы стенда удалены.

## Удаление

Закройте SSH-туннели через `Ctrl+C`. Если IAM-токен истёк, получите новый
в той же сессии PowerShell. После подтверждения удаления очистите токен.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py destroy
if ($LASTEXITCODE -ne 0) { throw 'Удаление стенда не подтверждено' }
Remove-Item Env:YC_TOKEN -ErrorAction SilentlyContinue
```

Инфраструктура описана в [Terraform](terraform/), настройка служб в
[Ansible](ansible/), команды управления в [scripts/](scripts/).
