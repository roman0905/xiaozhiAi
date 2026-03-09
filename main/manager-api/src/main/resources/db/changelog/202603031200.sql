-- 新增血糖数据查询插件
INSERT INTO ai_model_provider (id, model_type, provider_code, name, fields,
                               sort, creator, create_date, updater, update_date)
VALUES ('SYSTEM_PLUGIN_GLUCOSE',
        'Plugin',
        'get_glucose_data',
        '血糖数据查询',
        JSON_ARRAY(
                JSON_OBJECT(
                        'key', 'api_url',
                        'type', 'string',
                        'label', '血糖数据 API 地址',
                        'default', 'http://192.168.0.22:8080/api/sensor/sensor/readings'
                ),
                JSON_OBJECT(
                        'key', 'api_key',
                        'type', 'string',
                        'label', 'API 密钥',
                        'default', '123456'
                )
        ),
        100, 0, NOW(), 0, NOW());
