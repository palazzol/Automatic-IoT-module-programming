#!/usr/bin/env perl

use strict;
use warnings;


use Data::Dumper;
use JSON::Parse 'json_file_to_perl';

my $networks = json_file_to_perl ('networks.json');

for my $key (keys %$networks) {
		print "network $key:\n";
		my $network = $networks->{$key};
		printf "    '%s' => '%s'\n", $network->{'ssid'}, $network->{'pass'};
}

print Dumper($networks->{'mark'});

print $networks->{'mark'}->{'mqtt_host'} . "\n";
