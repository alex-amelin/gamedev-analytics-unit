---
title: "Review: PRD Grounding on directaiq Code"
status: final
created: 2026-05-23
reviewed_by: Code search specialist (ad-hoc)
---

# Review: PRD Grounding on directaiq Code

## Вердикт

**OVERALL: PASS with CRITICAL+HIGH findings.** Аддендум корректно цитирует код directaiq; FR формально верны, но есть существенные недооценённые зависимости: (1) MetricaClient прямо вызывает AuthManager→ConfigManager — разрыв в FR-4 нетривиален; (2) `_COST_COLUMN_SEMANTICS` в MCP глубже заложен в regex-fallback; (3) мета-таблица состояния в directaiq не используется — это новое в нашем контексте.

---

## Findings

### 1. **[CRITICAL]** MetricaClient→AuthManager→ConfigManager скрытая цепочка

**Severity:** CRITICAL  
**Location:** Аддендум §A; код directaiq `metrica_client.py:141`, `auth_manager.py:387`

**Факт:** MetricaClient.__init__ вызывает `AuthManager.get_metrica_credentials()` (line 141), которая импортирует ConfigManager (line 370) и использует fallback на YANDEX_DIRECT_TOKEN.

**Аддендум:** Верно описывает fallback, но не подчёркивает, что вендоринг клиента тянет ConfigManager с собой.

**PRD FR-4:** Требует «тонкий env-ридер» БЕЗ Direct-fallback и БЕЗ ConfigManager.

**Suggest Fix:** Написать свой GameDevAuthManager, не вендорить auth_manager.py целиком.

---

### 2. **[HIGH]** Мета-таблица состояния в directaiq НЕ используется для чекпойнта

**Severity:** HIGH  
**Location:** Аддендум §C; код `p81_load_logs.py:265–285`

**Факт:** `_get_loaded_dates_duckdb()` вычисляет состояние через `SELECT DISTINCT date`, не читает `table_metadata`.

**Аддендум:** Правильно отмечает, что загрузчик логов не использует table_metadata. ✓

**Но:** FR-12 требует мета-таблицу как источник истины с реконсиляцией против фактических партиций. Это **новая архитектура**, не наследование.

**Suggest Fix:** Спроектировать собственную схему мета-таблицы (день/источник/row_count/status) с явной реконсиляцией.

---

### 3. **[HIGH]** `_COST_COLUMN_SEMANTICS` имеет regex-fallback, требует переосмысления

**Severity:** HIGH  
**Location:** Аддендум §D; код `core.py:26–48`

**Факт:** Есть явный словарь `_COST_COLUMN_SEMANTICS` (таблица), но также `_GENERIC_MONEY_COL_RE` (regex), который ловит любые column matching `(cost|.*_revenue)`.

**Аддендум:** Упоминает только явный словарь.

**PRD FR-18:** Говорит о «лёгкой доработке = замена _COST_COLUMN_SEMANTICS». Regex-fallback требует переосмысления для геймдева (вероятно, ненужен).

**Suggest Fix:** Оценить при реализации, нужна ли regex-монетизация; переосмыслить паттерны (VAT не применим к Метрике).

---

### 4. **[MEDIUM]** Goal-плейсхолдеры связаны с ConfigManager

**Severity:** MEDIUM  
**Location:** Аддендум §D; код `core.py:97–99`

**Факт:** MCP читает goal_ids из ConfigManager и подставляет в `{{PRIMARY_GOAL_ID}}`.

**FR-18:** Требует убрать Direct-goal-плейсхолдеры. Это нетривиально: требует переписать контекст-генерацию.

**Suggest Fix:** Явно перечислить удаляемые плейсхолдеры; спланировать v2 расширение для геймдев-плейсхолдеров.

---

### 5. **[MEDIUM]** Init-команда: документация неточна (10 шагов vs 9)

**Severity:** MEDIUM (документационная)  
**Location:** Аддендум §E; код `init_project.nu:286–346`

**Факт:** Код имеет 9 основных функций; аддендум перечисляет 10 пунктов (разделены copy-template и миграции, install-dependencies и uv sync).

**Suggest Fix:** Уточнить в аддендуме, что это 9 логических шагов.

---

### 6. **[MEDIUM]** ID-колонки требуют HUGEINT, не явно в FR-6/FR-7

**Severity:** MEDIUM  
**Location:** Аддендум §C; код `p81_load_logs.py:100–102`; PRD FR-2, FR-6, FR-7

**Факт:** visitID/clientID/watchID требуют HUGEINT (> 2^63).

**Аддендум/PRD:** Упомянуто в таблице, но не в FR.

**Suggest Fix:** При фиксации Open Question #1 явно перечислить HUGEINT-требующие колонки.

---

## Таблица findings

| Severity | Issue | FR | Code Location | Suggested Action |
|----------|-------|----|----|---|
| CRITICAL | MetricaClient→ConfigManager цепь | FR-4 | metrica_client.py:141 | Собственный env-ридер без fallback |
| HIGH | Мета-таблица не источник истины в directaiq | FR-12 | p81_load_logs.py:265–285 | Спроектировать новую мета-таблицу |
| HIGH | `_COST_COLUMN_SEMANTICS` + regex-fallback | FR-18 | core.py:26–48 | Переосмыслить семантики денег |
| MEDIUM | Goal-плейсхолдеры требуют переписи | FR-18 | core.py:97–99 | Явно перечислить удаляемые плейсхолдеры |
| MEDIUM | Init-команда: 10 шагов vs 9 в коде | FR-19,20 | init_project.nu:286–346 | Уточнить в документации |
| MEDIUM | HUGEINT для ID колонок не явен | FR-2,6,7 | p81_load_logs.py:100–102 | Указать в Open Question #1 |

---

## Скрытые зависимости

1. **MetricaClient вендоринг:** Клиент сразу вызывает AuthManager, которая дёргает ConfigManager. Требуется собственная реализация без этой цепи.

2. **Мета-таблица состояния:** В directaiq это побочный продукт BaseScript, в нашем контексте это первоклассный механизм с реконсиляцией.

3. **MCP schema-agnostic claim:** На деле имеет hardcoded Direct-семантику денег + regex-fallback, требует полного пересмотра.

4. **Goal-плейсхолдеры:** Если в v2+ нужны геймдев-плейсхолдеры, придётся расширять либо ConfigManager (нарушает FR-4), либо контекст-генерацию.

---

## Рекомендации для архитектуры

1. Разорвать MetricaClient→ConfigManager до старта FR-4.
2. Спроектировать мета-таблицу отдельно от BaseScript-паттерна.
3. Перепроверить при сборке MCP, какие колонки попадают под `_annotate_money_column()`.
4. Явно перечислить HUGEINT-требующие ID при фиксации Open Question #1.

