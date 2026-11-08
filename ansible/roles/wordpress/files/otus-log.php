<?php
/**
 * Plugin Name: OTUS request journal
 */
add_action('template_redirect', static function (): void {
    if (!is_home() && !is_singular('post')) {
        return;
    }
    $marker = $_SERVER['HTTP_X_OTUS_CHECK'] ?? '';
    if (!preg_match('/\A[a-f0-9]{32}\z/D', $marker)) {
        $marker = '';
    }
    $event = wp_json_encode([
        'event' => is_home() ? 'portal_opened' : 'article_opened',
        'marker' => $marker,
    ]);
    if ($event !== false && !error_log($event . PHP_EOL, 3, '/var/log/wordpress/requests.log')) {
        error_log('OTUS: WordPress request journal is unavailable');
    }
});
