# Файл: config/pyscript/pressure_monitor.py (или config/pyscript/apps/pressure_monitor.py)

# --- КОНФИГУРАЦИЯ ---
SENSOR_ID = "sensor.openweathermap_pressure" # ID вашего сенсора давления
INFLUXDB_MEASUREMENT = "hPa"                 # Измерение (таблица) в InfluxDB
INFLUXDB_FIELD = "value"                     # Поле со значением сенсора

# Сервис для ручных запросов /trend
TELEGRAM_NOTIFIER_SERVICE = "notify.telegram" 

# Chat ID для автоматических уведомлений (!!! ОБЯЗАТЕЛЬНО ЗАМЕНИТЕ !!!)
TELEGRAM_AUTO_ALERT_CHAT_ID = "ВАШ_ОСНОВНОЙ_CHAT_ID_ДЛЯ_АВТО_УВЕДОМЛЕНИЙ" # Например, "474773815"

# Порог для выделения "резкого" изменения в сообщении ручного запроса /trend
SHARP_CHANGE_THRESHOLD_HPA_PER_HOUR_MANUAL = 1.0 

# ID Input Helpers для автоматических уведомлений
AUTO_THRESHOLD_HELPER = "input_number.auto_pressure_alert_threshold"
AUTO_WINDOW_HELPER = "input_number.auto_pressure_check_window_hours"
AUTO_LAST_ALERT_HELPER = "input_datetime.auto_pressure_last_alert_time"

# Кулдаун для автоматических уведомлений в минутах
AUTO_ALERT_COOLDOWN_MINUTES = 180 # Например, 3 часа

def _get_pressure_data_from_influx(time_window_hours_int):
    """
    Запрашивает данные о давлении из InfluxDB за указанное окно.
    Возвращает словарь с данными или None при ошибке.
    Предполагается, что сервис influxdb.query работает.
    """
    query = f"""
    SELECT 
        first("{INFLUXDB_FIELD}") AS p_start, 
        last("{INFLUXDB_FIELD}") AS p_end,
        mean("{INFLUXDB_FIELD}") AS p_mean,
        min("{INFLUXDB_FIELD}") AS p_min,
        max("{INFLUXDB_FIELD}") AS p_max
    FROM "{INFLUXDB_MEASUREMENT}"
    WHERE "entity_id" = '{SENSOR_ID}' AND time >= now() - {time_window_hours_int}h
    """
    log.info(f"Pyscript (_get_pressure_data): Запрос к InfluxDB:\n{query}")

    try:
        query_result_list = service.call("influxdb", "query", query=query, return_response=True)
        log.debug(f"Pyscript (_get_pressure_data): Ответ от influxdb.query: {query_result_list}")

        if not (isinstance(query_result_list, list) and query_result_list and 
                "values" in query_result_list[0] and query_result_list[0]["values"]):
            log.warning(f"Pyscript (_get_pressure_data): Нет данных от InfluxDB за {time_window_hours_int} ч.")
            return None

        series_data = query_result_list[0]
        values_row = series_data["values"][0]
        columns = series_data.get("columns", [])

        def get_val(name):
            try:
                val = values_row[columns.index(name)]
                return float(val) if val is not None else None
            except (ValueError, IndexError, TypeError): return None

        p_start, p_end = get_val("p_start"), get_val("p_end")
        if p_start is None or p_end is None:
            log.warning(f"Pyscript (_get_pressure_data): Недостаточно данных (p_start/p_end) за {time_window_hours_int} ч.")
            return None
        
        p_mean = get_val("p_mean") if get_val("p_mean") is not None else p_end
        p_min = get_val("p_min") if get_val("p_min") is not None else min(p_start, p_end)
        p_max = get_val("p_max") if get_val("p_max") is not None else max(p_start, p_end)
        
        return {
            "p_start": p_start, "p_end": p_end, "p_mean": p_mean,
            "p_min": p_min, "p_max": p_max, "hours": time_window_hours_int
        }

    except Exception as e:
        import traceback 
        log.error(f"Pyscript (_get_pressure_data): ОШИБКА запроса/обработки InfluxDB: {e}\n{traceback.format_exc()}")
        return None

def _format_pressure_message(pressure_data, threshold_for_alert_text, for_auto_alert=False):
    """
    Формирует текстовое сообщение на основе данных о давлении.
    """
    p_start, p_end, p_mean = pressure_data["p_start"], pressure_data["p_end"], pressure_data["p_mean"]
    p_min, p_max, hours = pressure_data["p_min"], pressure_data["p_max"], pressure_data["hours"]

    pressure_delta = p_end - p_start
    rate_hpa_per_hour = (pressure_delta / hours) if hours > 0 else 0

    h_word = "час" if hours%10==1 and hours%100!=11 else \
             "часа" if hours%10 in [2,3,4] and hours%100 not in [12,13,14] else "часов"
    
    dir_w, trend_icon = "изменилось на", "➡️"
    if rate_hpa_per_hour > 0.05: dir_w, trend_icon = "выросло на", "📈"
    elif rate_hpa_per_hour < -0.05: dir_w, trend_icon = "упало на", "📉"
    
    curr_p_state = state.get(SENSOR_ID)
    curr_p_str = f"{float(curr_p_state.state):.1f} гПа" if curr_p_state and curr_p_state.state not in ["unknown","unavailable"] else "N/A"
    
    title_prefix = "⚠️ *АВТО-УВЕДОМЛЕНИЕ О ДАВЛЕНИИ* ⚠️\n" if for_auto_alert else ""
    is_sharp_change = abs(rate_hpa_per_hour) >= threshold_for_alert_text
    sharp_alert_msg_part = f"более {threshold_for_alert_text:.1f} гПа/час"
    
    msg_parts = [
        f"{title_prefix}{trend_icon} *Анализ давления за {hours} {h_word}*",
        f" • Период: `{p_start:.1f} гПа` → `{p_end:.1f} гПа`",
        f" • Изменение: {dir_w} `{abs(pressure_delta):.1f} гПа`",
        f" • Ср. скорость: `{rate_hpa_per_hour:.1f} гПа/час`",
        f" • Мин/Макс/Сред: `{p_min:.1f}` / `{p_max:.1f}` / `{p_mean:.1f} гПа`",
        f" • Текущее (сенсор): `{curr_p_str}`"
    ]

    if is_sharp_change:
        alert_text = f"\n\n*Внимание! Резкое изменение ({sharp_alert_msg_part})!*"
        msg_parts.append(alert_text)
        
    final_msg = "\n".join(msg_parts)
        
    return final_msg, rate_hpa_per_hour, is_sharp_change


def _send_telegram_message_pyscript(chat_id, message, parse_mode=None, notifier_service=TELEGRAM_NOTIFIER_SERVICE):
    if not chat_id: log.error("Pyscript (_send_telegram_message_pyscript): chat_id не предоставлен."); return
    try:
        domain, service_name = notifier_service.split('.', 1)
        s_data = {"message": message, "data": {"target": str(chat_id)}}
        if parse_mode: s_data["data"]["parse_mode"] = parse_mode
        service.call(domain, service_name, **s_data)
    except Exception as e1:
        log.warning(f"Pyscript: Ошибка отправки через {notifier_service}: {e1}. Пробую telegram_bot.send_message.")
        try:
            s_data_direct = {"target": int(chat_id), "message": message}
            if parse_mode: s_data_direct["parse_mode"] = parse_mode
            service.call("telegram_bot", "send_message", **s_data_direct)
        except Exception as e2: log.error(f"Pyscript: Ошибка отправки сообщения: {e2}")

# --- СЕРВИСЫ PYSCRIPT ---

@service
def get_pressure_analysis_for_telegram(hours_str=None, chat_id=None, user_id=None):
    log.info("--------------------------------------------------")
    log.info(f"Pyscript: Ручной запрос /trend: hours_str='{hours_str}', chat_id='{chat_id}'")
    if not chat_id: return
    if hours_str is None or (isinstance(hours_str, str) and not hours_str.strip()):
        _send_telegram_message_pyscript(chat_id, "Укажите часы для анализа. Пример: /trend 2")
        return

    time_window_hours = 0
    try:
        time_window_hours = int(hours_str)
        if not (0 < time_window_hours <= 720): raise ValueError("Часы вне диапазона")
    except ValueError:
        _send_telegram_message_pyscript(chat_id, f"Ошибка: '{hours_str}' - некорректное кол-во часов.")
        return
    
    pressure_data = _get_pressure_data_from_influx(time_window_hours)
    if pressure_data is None:
        _send_telegram_message_pyscript(chat_id, "Не удалось получить данные о давлении из InfluxDB для ручного запроса.")
        return

    final_msg, _, _ = _format_pressure_message(pressure_data, threshold_for_alert_text=SHARP_CHANGE_THRESHOLD_HPA_PER_HOUR_MANUAL, for_auto_alert=False)
    _send_telegram_message_pyscript(chat_id, final_msg, parse_mode="Markdown")
    log.info("Pyscript: Ручной отчет отправлен.")


@service
def check_pressure_for_auto_alert():
    log.info("--------------------------------------------------")
    log.info("Pyscript: Авто-проверка давления ЗАПУЩЕНА")

    try:
        threshold_hpa_per_hour_auto = float(state.get(AUTO_THRESHOLD_HELPER).state)
        time_window_hours_auto = int(float(state.get(AUTO_WINDOW_HELPER).state)) 
    except Exception as e:
        log.error(f"Pyscript (Авто-проверка): Ошибка чтения настроек из Input Helpers: {e}")
        return

    log.info(f"Pyscript (Авто-проверка): Порог={threshold_hpa_per_hour_auto} гПа/ч, Окно={time_window_hours_auto} ч.")

    now_ts = task.executor.time()
    last_alert_dt_state = state.get(AUTO_LAST_ALERT_HELPER)
    if last_alert_dt_state and last_alert_dt_state.state != 'unknown':
        try:
            from datetime import datetime 
            last_alert_dt_obj = datetime.fromisoformat(last_alert_dt_state.state)
            last_alert_ts = last_alert_dt_obj.timestamp()
            if (now_ts - last_alert_ts) < (AUTO_ALERT_COOLDOWN_MINUTES * 60):
                log.info(f"Pyscript (Авто-проверка): Кулдаун. Последнее уведомление: {last_alert_dt_state.state}.")
                return
        except Exception as e:
            log.warning(f"Pyscript (Авто-проверка): Ошибка обработки времени последнего уведомления ('{last_alert_dt_state.state}'): {e}.")
    else:
        log.info(f"Pyscript (Авто-проверка): Время последнего авто-уведомления не установлено.")

    pressure_data = _get_pressure_data_from_influx(time_window_hours_auto)
    if pressure_data is None:
        log.warning("Pyscript (Авто-проверка): Нет данных InfluxDB для авто-проверки.")
        return

    final_msg, rate_hpa_per_hour, is_sharp_change = _format_pressure_message(
        pressure_data, 
        threshold_for_alert_text=threshold_hpa_per_hour_auto, 
        for_auto_alert=True
    )

    if is_sharp_change: 
        log.info(f"Pyscript (Авто-проверка): ОБНАРУЖЕНО РЕЗКОЕ ИЗМЕНЕНИЕ! Скорость: {rate_hpa_per_hour:.1f} гПа/ч.")
        _send_telegram_message_pyscript(TELEGRAM_AUTO_ALERT_CHAT_ID, final_msg, parse_mode="Markdown")
        try:
            from datetime import datetime
            dt_obj_to_set = datetime.now() 
            service.call("input_datetime", "set_datetime", entity_id=AUTO_LAST_ALERT_HELPER, 
                         datetime=dt_obj_to_set.isoformat(sep=' ', timespec='seconds'))
            log.info(f"Pyscript (Авто-проверка): Время последнего авто-уведомления обновлено.")
        except Exception as e_dt:
            log.error(f"Pyscript (Авто-проверка): Ошибка обновления времени последнего уведомления: {e_dt}")
    else:
        log.info(f"Pyscript (Авто-проверка): Изменение давления ({rate_hpa_per_hour:.1f} гПа/ч) в норме.")
    log.info("Pyscript: Авто-проверка давления ЗАВЕРШЕНА")
    log.info("--------------------------------------------------")