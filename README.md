# ДЗ08. Очередь журналов Kafka

## Цель работы

Стенд собирает журналы Nginx и WordPress, хранит их в двух топиках Kafka
и показывает события в Kibana. Logstash читает топики и пишет в отдельные
индексы Elasticsearch.

## Архитектура

| ВМ | Компоненты | Начальные параметры |
| --- | --- | --- |
| `portal` | Nginx, PHP-FPM, WordPress, MariaDB, Filebeat | 2 vCPU, 4 ГБ RAM, HDD 20 ГБ |
| `logs` | Два брокера Kafka с одним контроллером KRaft, Elasticsearch, Logstash, Kibana, Filebeat | 2 vCPU, 12 ГБ RAM, HDD 20 ГБ |

Обе ВМ используют Ubuntu 24.04 x86-64 и приватную сеть Yandex Cloud.
Terraform создаёт инфраструктуру. Ansible запускается на ВМ портала и
настраивает обе ВМ. Настройки размещения находятся в [config/lab.json](config/lab.json),
версии служб и параметры памяти в [config/stack.yml](config/stack.yml).

```text
Nginx и WordPress → Filebeat → Kafka → Logstash → Elasticsearch → Kibana
```

## Соответствие заданию

| Требование | Реализация |
| --- | --- |
| Kafka на одной ноде | Два процесса брокеров с разными портами и каталогами на одной ВМ. Фактор репликации 2 требует двух брокеров. |
| Топики | `nginx` и `wordpress`, у каждого две партиции и две реплики. |
| Сбор журналов | Filebeat установлен на обеих ВМ. На портале он читает отдельные журналы доступа Nginx и WordPress. На второй ВМ агент установлен без активного сбора. |
| ELK на одной ноде | Logstash читает Kafka и пишет в `logs-nginx` и `logs-wordpress`. В Kibana есть два представления данных с `@timestamp`. |

## Подготовка

Нужны Windows x64, PowerShell 5.1 или 7, Git, системный Python 3.12–3.14, OpenSSH,
[Terraform](https://developer.hashicorp.com/terraform/install) от 1.6 и
[Yandex Cloud CLI](https://yandex.cloud/ru/docs/cli/quickstart) в PATH.
С ВМ нужен исходящий доступ к репозиториям Ubuntu, Apache Kafka, WordPress
и GitHub Releases WP-CLI. Ansible загружает пакеты Elastic с зеркала
Яндекса и проверяет их по SHA-512, опубликованной Elastic для выбранной версии.

Подготовьте отдельный каталог YC и сервисный аккаунт с правами управления
Compute Cloud и VPC в этом каталоге. Для выделенного учебного каталога
можно назначить сервисному аккаунту `editor` на каталог и `quota-manager.viewer`
на облако. Оператору нужна роль `iam.serviceAccounts.tokenCreator` на этот
сервисный аккаунт. Вход в `yc` выполняется от имени оператора.
[Авторизация Terraform](https://yandex.cloud/ru/docs/terraform/authentication) ·
[Просмотр квот](https://yandex.cloud/ru/docs/quota-manager/quickstart)

Клонируйте ветку с решением и откройте корень клона в PowerShell.

```powershell
git clone --branch kafkadz08 https://github.com/airiga1897/OTUS_DZ08.git
Set-Location OTUS_DZ08
.\scripts\setup.ps1
. .\scripts\env.ps1
```

Подготовка использует Poetry 2.4.1 и окружение `.venv`. Скрипт окружения
запрашивает идентификаторы облака, каталога и сервисного аккаунта и получает
временный IAM-токен в текущую сессию. Terraform и Python получают его из
окружения. Для новой сессии или после истечения токена повторите `env.ps1`.

## Развёртывание и импорт

```powershell
.\.venv\Scripts\python.exe scripts/lab.py deploy
.\.venv\Scripts\python.exe scripts/lab.py import-content
```

`deploy` проверяет квоты и совместимость размещения. Сначала ищет зону для
обеих ВМ. Если квот одной зоны недостаточно, ищет две зоны одной VPC. Для
каждой ВМ создаётся своя подсеть, маршрут через NAT есть только у `logs`. Если
допустимого размещения нет, команда выводит причины до создания ресурсов.
Вход по SSH на портал разрешён только с подтверждённых публичных IPv4 `/32`.
При пустом `admin_cidrs` команда определяет адрес по двум независимым
HTTPS-сервисам. Если роутер или VPN может направлять SSH через разные шлюзы,
задайте все подтверждённые публичные `/32` в локальном `config/lab.local.json`.
Ниже приведён шаблон файла. Замените пустой список своими адресами.

```json
{
  "admin_cidrs": []
}
```

Этот файл исключён из Git. Для уже созданного стенда используется список
из сохранённого контекста. Контекст, Terraform state, SSH-ключи и пароли
хранятся в `.local/` или `.local-test/` и также исключены из Git.

После `deploy` доступен WordPress. Отдельная команда `import-content`
загружает [WXR](portal/materials.xml). Выгрузка содержит материалы ДЗ01–ДЗ07
из [фикстуры портала ДЗ07](https://github.com/airiga1897/OTUS_DZ07/blob/d1bb8b9ec6192c0e250487497f890ce8ec93099c/app/knowledge/fixtures/initial_data.json) и новый материал ДЗ08. Повторный импорт сохраняет
уже внесённые правки и добавляет недостающие записи. После загрузки на
главной странице доступны восемь плиток. Повторный `deploy` сохраняет
расположение ВМ, ключи и содержимое портала.

## Проверка и открытие

```powershell
.\.venv\Scripts\python.exe scripts/lab.py verify
.\.venv\Scripts\python.exe scripts/lab.py open
```

`verify` проверяет плитки, два топика, синхронизацию реплик и представления
данных. Затем временно останавливает Logstash, отправляет контрольный
HTTP-запрос и проверяет оба события в Kafka. После восстановления Logstash
команда ждёт появления событий в соответствующих индексах.

`open` выводит зоны размещения и открывает SSH-туннели на
`http://localhost:8080` и `http://localhost:5601`. Оставьте окно PowerShell
открытым. В Kibana войдите как `elastic`, в WordPress как `otus-admin`.
Пароли находятся в локальном `.local/secrets.json` в полях
`elastic_password` и `wordpress_admin`. Чтобы скопировать пароль Kibana
без вывода в консоль, используйте отдельное окно PowerShell.

```powershell
(Get-Content -Raw .local/secrets.json | ConvertFrom-Json).elastic_password | Set-Clipboard
```

В Kibana Discover по очереди откройте **ДЗ08 nginx** и **ДЗ08 wordpress**
за последние 15 минут. Для запроса `otus.marker: *` должны отображаться
соответственно события `http_access` и `portal_opened`. В журналах
сохраняются тип события, тестовый идентификатор и для Nginx метод и
HTTP-статус. Реквизиты, cookies и строки запросов в журналы не попадают.

## Удаление

```powershell
.\.venv\Scripts\python.exe scripts/lab.py destroy
. .\scripts\env.ps1 -Clear
```

`destroy` сверяет область YC и принадлежащие стенду ресурсы. При ошибке
оставляет state, ключи и контекст для повторной попытки. При успешном
удалении очищает эти файлы. Временный `test-cycle` использует
отдельный контекст `.local-test` и удаляет свой стенд после проверки.
Для очистки после прерванного цикла выполните `scripts/lab.py destroy --test-context`
тем же интерпретатором.

## Подтверждённые результаты

| Проверка | Локальный результат |
| --- | --- |
| Подготовка в чистом каталоге и повторный `setup` | Пройдены на Windows PowerShell с Python 3.14.2. |
| `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` | 14 тестов, включая размещение, локальные правила доступа, повтор импорта и ограничения удаления. |
| `terraform -chdir=terraform validate` и `terraform -chdir=terraform test` после `init` | Конфигурация валидна, два сценария со смоделированным провайдером пройдены на Terraform 1.13.5. |

В Yandex Cloud подтверждены получение IAM-токена, создание двух ВМ в зоне
`ru-central1-d` и подключение по SSH с разрешённого публичного IPv4 `/32`.
Для этого прогона использован токен профиля оператора `yc`.
После отдельной команды `import-content` на главной странице WordPress
проверены восемь плиток.

В каждом топике Kafka проверены две партиции и две синхронные реплики.
При остановленном Logstash события одного помеченного запроса появились в обоих
топиках. После запуска Logstash события найдены в двух индексах Elasticsearch.
Через API Kibana проверены представления данных с `@timestamp` и поиск того же
запроса в `logs-nginx` и `logs-wordpress`. Ручной просмотр в Discover в этом
прогоне не выполнялся.

После проверки стенд удалён. Отсутствие его ресурсов подтверждено по Terraform
state и API Yandex Cloud.

## Ограничения

Отключение ВМ `logs` останавливает оба брокера и весь ELK. Журналы портала
буферизуются на диске Filebeat. Хранение в Kafka ограничено по времени и объёму.
Индексы Elasticsearch имеют одну первичную копию. Схема рассчитана на учебный
запуск двух ВМ.

## Структура репозитория

| Путь | Назначение |
| --- | --- |
| [terraform/](terraform/) | ВМ, сеть, маршруты и правила доступа |
| [ansible/](ansible/) | Настройка портала, Kafka и ELK |
| [scripts/](scripts/) | Подготовка окружения, команды управления и адаптер Yandex Cloud |
| [config/](config/) | Параметры размещения и версии компонентов |
| [portal/](portal/) | Учебные материалы в формате WXR |
| [tests/](tests/) | Локальные проверки размещения, импорта и очистки |
