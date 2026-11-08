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

На Windows используйте PowerShell 5.1 или 7. Заранее установите Git,
Python 3.12–3.14 и OpenSSH. Оператору нужны права на управление Compute Cloud
и VPC в каталоге и просмотр квот облака.
При одном стабильном публичном IPv4 для SSH и доступа из браузера локальный
файл `config/lab.local.json` создавать не нужно. При нескольких выходах
перечислите их подтверждённые публичные IPv4 `/32` в массиве `admin_cidrs`
этого файла перед `deploy`.

Клонируйте репозиторий. Git откроет ветку по умолчанию.

```powershell
git clone https://github.com/airiga1897/OTUS_DZ08.git
Set-Location OTUS_DZ08
```

Если для проверки нужна другая ветка или в открытой ветке нет файлов стенда,
посмотрите список и переключитесь до подготовки окружения. В `git switch`
укажите имя ветки без префикса `remotes/origin/`.

```powershell
git branch --all
git switch 'ИМЯ_ВЕТКИ'
```

Дальнейшие команды выполняйте из корня клона. `setup.ps1` подготавливает
`.venv` через Poetry. Если Terraform или Yandex Cloud CLI отсутствует в `PATH`
и в `.tools/bin`, скрипт скачивает его в `.tools/bin`. Для уже установленного
Terraform требуется версия от 1.6 до 2.0, не включая 2.0. При запуске через
`lab.py` провайдер по умолчанию устанавливается с зеркала Yandex Cloud.

```powershell
.\scripts\setup.ps1
```

Для входа в YC настройте профиль этого клона. `lab.py` берёт ID облака,
ID каталога и временный IAM-токен из `.tools/yc-config.yaml` при `deploy`
и `destroy`. Если профиль ещё не настроен, запустите мастер и выберите
облако и каталог стенда. Для уже созданного стенда выберите прежние облако
и каталог.

```powershell
$ycExe = (Get-Command yc.exe, .\.tools\bin\yc.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$ycConfig = Join-Path $PWD '.tools\yc-config.yaml'
$yc = { & $ycExe --config $ycConfig @args }.GetNewClosure()
& $yc init --no-browser
```

`deploy` проверяет квоты перед созданием стенда. При пустом `admin_cidrs`
он добавляет один `/32`, если два HTTPS-сервиса подтвердили один и тот же
публичный IPv4. Разрешёнными адресами ограничены SSH, WordPress на порту 8080
и HTTPS-доступ к Kibana на порту 5601. Kibana работает на приватной ВМ `logs`
и доступна через `portal`.
Для ранее созданного стенда повторите `deploy`. Если план требует замены
или удаления ресурсов, команда остановится до применения.
После успешного развёртывания отдельно загрузите восемь материалов WordPress.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py deploy
if ($LASTEXITCODE -ne 0) { throw 'Развёртывание не завершилось' }
.\.venv\Scripts\python.exe scripts/lab.py import-content
```

Если архив провайдера подготовлен заранее, поместите его в
`.tools/providers/registry.terraform.io/yandex-cloud/yandex/`. Для Windows x64
используйте `terraform-provider-yandex_0.244.0_windows_amd64.zip`. Terraform
возьмёт пакет с диска и сверит его с `.terraform.lock.hcl`. Для самого стенда
по-прежнему нужен доступ к Yandex Cloud.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py deploy --plugin-dir .\.tools\providers
```

## Проверка

```powershell
.\.venv\Scripts\python.exe scripts/lab.py verify
.\.venv\Scripts\python.exe scripts/lab.py open
```

`verify` проверяет восемь плиток по публичному адресу WordPress, параметры
топиков и прохождение событий через Kafka и Logstash в Elasticsearch.
`open` выводит адреса для браузера и отпечаток SHA-256 сертификата Kibana.

1. Откройте WordPress по адресу из `open`. На главной должно быть восемь
   плиток. Откройте любую статью и проверьте, что адрес остался публичным.
2. Откройте HTTPS-адрес Kibana. Сертификат стенда самоподписанный. Перед
   входом сравните отпечаток SHA-256 сертификата в браузере с выводом
   `open`. При совпадении разрешите доступ к этому адресу и войдите как
   `elastic`. Команда копирует пароль в буфер обмена.

   ```powershell
   (Get-Content -Raw .local/secrets.json | ConvertFrom-Json).elastic_password | Set-Clipboard
   ```

3. В **Discover** выберите последние 15 минут. В **ДЗ08 nginx** найдите
   событие `http_access` с непустым `otus.marker`. В **ДЗ08 wordpress**
   найдите событие `portal_opened` с тем же значением `otus.marker`.

В облачном прогоне главная страница WordPress с восемью плитками и статья
открылись по публичному адресу без перенаправлений. Публичный адрес был
только у `portal`. Правила доступа к портам 22, 8080 и 5601 разрешали
подключения только с подтверждённых IPv4 `/32`.

Команда `verify` подтвердила две партиции и две синхронные реплики в каждом
топике. Kafka принимала события при остановленном Logstash, а после его запуска
события появились в Elasticsearch. Через API Kibana по публичному HTTPS
с проверкой сертификата и авторизацией найдены события одного контрольного
запроса в обоих индексах. Kibana отклонила запрос к API без авторизации,
а прокси отклонил запрос по обычному HTTP. Визуальная проверка в Discover
остаётся ручным шагом. Удаление ресурсов подтверждено по Terraform state
и API Yandex Cloud.

## Удаление

Удалите стенд из того же клона. После подтверждения удаления ресурсов
команда очищает локальные данные стенда и содержимое `.tools/temp`.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py destroy
```

Для разбора стенда с заранее подготовленным провайдером укажите тот же каталог.

```powershell
.\.venv\Scripts\python.exe scripts/lab.py destroy --plugin-dir .\.tools\providers
```

После успешного `destroy` можно удалить настройки YC и сохранённые данные
входа этого клона. Выполните команду из корня клона.

```powershell
Remove-Item -LiteralPath .\.tools\yc-config.yaml
```

Инфраструктура описана в [Terraform](terraform/), настройка служб в
[Ansible](ansible/), команды управления в [scripts/](scripts/).
