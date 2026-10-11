<?php
/**
 * Plugin Name: Creative Mushroom Health Check hand-off
 * Description: When someone submits the Website Health Check form, asks GitHub to run the check. The report is emailed to Creative Mushroom for review, never straight to the visitor.
 * Version: 1.0
 * Author: Creative Mushroom
 *
 * Install: upload this file to wp-content/mu-plugins/ (create the folder if it is not there).
 * Then add these two lines to wp-config.php, above "That's all, stop editing!":
 *
 *   define( 'CM_GITHUB_REPO', 'your-github-name/cm-health-check' );
 *   define( 'CM_GITHUB_TOKEN', 'github_pat_...' );
 *
 * The token is a fine-grained GitHub token for that one repository only, with
 * "Contents: Read and write" permission. Keep it out of the database and out of email.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

// Gravity Forms form and field IDs for "Website health check request".
const CM_HC_FORM_ID = 3;
const CM_HC_FIELDS  = array(
	'url'           => '1',
	'contact_name'  => '2',
	'contact_email' => '3',
	'business_type' => '4',
	'name'          => '6', // Business name (optional field; fine if it is not there)
);

add_action( 'gform_after_submission_' . CM_HC_FORM_ID, 'cm_hc_dispatch', 10, 2 );

function cm_hc_dispatch( $entry, $form ) {
	if ( ! defined( 'CM_GITHUB_TOKEN' ) || ! defined( 'CM_GITHUB_REPO' ) ) {
		cm_hc_problem( $entry, 'The GitHub details are missing from wp-config.php, so the automatic check did not start.' );
		return;
	}
	if ( ! empty( $entry['status'] ) && 'spam' === $entry['status'] ) {
		return; // never run checks for spam entries
	}

	$val = function ( $key ) use ( $entry ) {
		return trim( (string) rgar( $entry, CM_HC_FIELDS[ $key ] ) );
	};

	$type = stripos( $val( 'business_type' ), 'hospitality' ) !== false ? 'hospitality' : 'other';

	$payload = array(
		'event_type'     => 'health-check',
		'client_payload' => array(
			'url'           => substr( $val( 'url' ), 0, 300 ),
			'name'          => substr( $val( 'name' ), 0, 80 ),
			'business_type' => $type,
			'contact_name'  => substr( $val( 'contact_name' ), 0, 120 ),
			'contact_email' => substr( $val( 'contact_email' ), 0, 200 ),
			'entry_id'      => (string) $entry['id'],
		),
	);

	$response = wp_remote_post(
		'https://api.github.com/repos/' . CM_GITHUB_REPO . '/dispatches',
		array(
			'timeout' => 15,
			'headers' => array(
				'Authorization'        => 'Bearer ' . CM_GITHUB_TOKEN,
				'Accept'               => 'application/vnd.github+json',
				'X-GitHub-Api-Version' => '2022-11-28',
				'User-Agent'           => 'creativemushroom.co.uk',
				'Content-Type'         => 'application/json',
			),
			'body'    => wp_json_encode( $payload ),
		)
	);

	$code = is_wp_error( $response ) ? 0 : (int) wp_remote_retrieve_response_code( $response );
	if ( 204 === $code ) {
		cm_hc_note( $entry, 'Automatic health check started. The report will be emailed for review.' );
		return;
	}
	$why = is_wp_error( $response ) ? $response->get_error_message() : 'GitHub replied with code ' . $code . '.';
	cm_hc_problem( $entry, 'The automatic health check could not start. ' . $why );
}

function cm_hc_note( $entry, $text ) {
	if ( class_exists( 'GFAPI' ) ) {
		GFAPI::add_note( $entry['id'], 0, 'Health check', $text );
	}
}

function cm_hc_problem( $entry, $text ) {
	cm_hc_note( $entry, $text );
	$site = rgar( $entry, CM_HC_FIELDS['url'] );
	wp_mail(
		get_option( 'admin_email' ),
		'Health check did not start: ' . $site,
		$text . "\n\nThe entry is safe in Gravity Forms (entry " . $entry['id'] . "). You can run the check by hand on your Mac, or from the Actions page on GitHub.\n"
	);
}
