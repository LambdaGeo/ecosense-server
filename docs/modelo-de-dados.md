# Modelo de dados

## `readings` — uma linha por mensagem recebida

| Coluna | Tipo | Origem | Descrição |
| --- | --- | --- | --- |
| `observed_at` | timestamptz | derivada | Tempo canônico da mensagem (ver `ts_source`) |
| `station_id` | text | token / `estacao_id` | Estação; na ingestão direta vem do token |
| `kind` | text | derivada | `reading` (há temperatura, umidade ou pressão válida) ou `event` |
| `event_key` | text | `evento_energia` | Valor do evento ou `''`; entra na deduplicação |
| `ts_source` | text | derivada | `gnss`, `device` ou `received` |
| `received_at` | timestamptz | servidor | Chegada (ou `data_servidor` da origem) |
| `ts_device_raw` | text | `timestamp_recebimento` | Relógio do dispositivo, como veio |
| `ts_gnss` | timestamptz | `data_gps` + `hora_gps` | Hora GNSS em UTC (nula sem fix ou ano < 2020) |
| `clock_skew_s` | float | derivada | Dispositivo − GNSS, em segundos |
| `temperatura_c` | float | DHT22 | Temperatura do ar (°C); faixa −40 a 80 |
| `umidade_relativa_perc` | float | DHT22 | Umidade relativa (%); faixa 0 a 100 |
| `pressao_atmosferica_hpa` | float | BMP180 | Pressão absoluta no nível da estação (hPa); faixa 300 a 1100 |
| `altitude_m` | float | BMP180 | Altitude barométrica com referência fixa de 1013,25 hPa |
| `temperatura_bmp_c` | float | BMP180 | Temperatura interna do BMP180 (°C) |
| `status_bateria_v` | float | ADC | Tensão da bateria (V), quando o firmware a enviar |
| `rssi` | float | ESP32 | Sinal Wi-Fi (dBm) |
| `gps_fix`, `latitude`, `longitude`, `altitude_gps_m`, `satellites`, `hdop` | — | NEO-6M | Posição e qualidade; posição nula sem fix ou em (0, 0) |
| `acc_x`…`gyro_z` | float | MPU6050 | Aceleração (g) e velocidade angular |
| `estado_energia`, `evento_energia`, `ciclo_deepsleep`, `motivo_despertar` | — | firmware | Estado e eventos do ciclo de sono profundo |
| `tempo_ativo_s`, `tempo_sleep_programado_s`, `duracao_sleep_anterior_s` | float | firmware | Tempos do ciclo, em segundos |
| `qc_flags` | jsonb | derivada | Campos anulados e motivo (`fora_da_faixa`, `posicao_nula`) |
| `source` | text | servidor | `ingest`, `bridge` ou `csv` |
| `payload_hash` | text | derivada | MD5 do payload sem campos do servidor |
| `raw` | jsonb | mensagem | Payload original completo |

Chave de unicidade: `(station_id, observed_at, kind, event_key)`.

## `sites` — instalações

`site_id`, `station_id`, `name`, `lat`, `lon`, `radius_m` (padrão 1500 m),
`height_m`, `valid_from`, `valid_to`, `notes`. Cada leitura com fix válido é
atribuída à instalação da mesma estação com o centro mais próximo dentro do
raio e do período de validade.

## Visões

| Visão | Conteúdo |
| --- | --- |
| `readings_located` | `readings` + `site_id`, `site_distance_m` e `observed_local` (hora civil America/Fortaleza) |
| `readings_hourly` | Por estação e hora: contagens (`n_records`, `n_temp`, `n_rh`, `n_p`) e médias, só de leituras |
| `gaps` | Intervalos > 5 min entre mensagens, com `programmed` para os pares de sono |

## Controle de acesso

| Tabela | Conteúdo |
| --- | --- |
| `stations` | Hash SHA-256 do token de ingestão de cada estação |
| `api_clients` | Hash da chave de leitura, estações permitidas (`NULL` = todas), validade e revogação |
| `access_log` | Cliente, rota, consulta, número de linhas e origem de cada leitura pela API |
