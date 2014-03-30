#!/usr/bin/env python2
# -*- coding: utf-8 -*-

'''
Concurrently open either Shodan search results, a specified IP range, a
single IP, or domain and print the status and title of the page.
Add the -u and -p options to attempt to login to the page.
Use -f to look for a certain string in the html response to check if
authentication succeeded. Will attempt to login first via a login form
and should that fail or not exist, login via HTTP Basic Auth.

eequires:   Linux
            Python 2.7
                gevents
                mechanize
                BeautifulSoup
                shodan


__author__ = Dan McInerney
             danmcinerney.org
             @danhmcinerney
'''

#This must be one of the first imports or else we get threading error on completion
from gevent import monkey
monkey.patch_all()

# Overzealously prevent mechanize's gzip warning
import warnings
warnings.filterwarnings("ignore")

import gevent
import argparse
import mechanize
from BeautifulSoup import BeautifulSoup
import cookielib
from socket import setdefaulttimeout
import re
from sys import exit
# Mechanize doesn't respsect timeouts when it comes to reading/waiting for SSL info so this is necessary
setdefaulttimeout(12)

# Including lxml in case someone wants to use it instead of BeautifulSoup
#import lxml
#from lxml.html import fromstring

def parse_args():
   """Create the arguments"""
   parser = argparse.ArgumentParser(
   formatter_class=argparse.RawDescriptionHelpFormatter,
   epilog='-----------------------------------------------------------------------------------\n'
          'Examples:\n\n'
          '  -Search Shodan for "dd-wrt" and print the title of each result\'s response page:\n\n'
          '      python device-pharmer.py -s "dd-wrt" -api Wutc4c3T78gRIKeuLZesI8Mx2ddOiP4\n\n'
          '  -Open a range of IP addresses, print the title of each response page and\n'
          '   make 100 requests concurrently:\n\n'
          '      python device-pharmer.py -t 192.168.0-5.1-254 -c 100\n\n'
          '  -Search Shodan for "dd-wrt" and attempt to login with "root" using password "admin"\n'
          '   then check the response page\'s html for the string ">Advanced Routing<":\n\n'
          '      python device-pharmer.py -s "dd-wrt" -u root -p admin -f ">Advanced Routing<"\n\n'
          '  -Open www.reddit.com specifically with https:// and attempt to login using "sirsmit418"\n'
          '   and password "whoopwhoop":\n\n'
          '      python device-pharmer.py -t www.reddit.com -ssl -u sirsmit418 -p whoopwhoop')

   parser.add_argument("-api", "--apikey", help="Your api key")
   parser.add_argument("-c", "--concurrent", default='1000', help="Enter number of concurrent requests to make; default = 1000")
   parser.add_argument("-f", "--findstring", help="Search html for a string; can be used to determine if a login was successful")
   parser.add_argument("-n", "--numpages", default='1', help="Number of pages deep to go in Shodan results with 100 results per page; default is 1")
   parser.add_argument("-p", "--password", help="Enter password after this argument")
   parser.add_argument("-s", "--shodansearch", help="Your search terms")
   parser.add_argument("-ssl", help="Test all the results using https:// rather than default http://", action="store_true")
   parser.add_argument("-t", "--targets", help="Enter an IP, a domain, or a range of IPs to fetch (e.g. 192.168.0-5.1-254 will"
                       "fetch 192.168.0.1 to 192.168.5.254; if using a domain include the subdomain if it exists: sub.domain.com or domain.com)")
   parser.add_argument("-u", "--username", help="Enter username after this argument")
   return parser.parse_args()

def shodan_search(search, apikey, pages):
    from shodan import WebAPI

    if apikey:
        API_KEY = apikey
    else:
        API_KEY = 'ENTER YOUR API KEY HERE AND KEEP THE QUOTES'

    api = WebAPI(API_KEY)

    ips_found = []

    try:
        results = api.search(search, page=1)
        total_results = results['total']
        print '[+] Results: %d' % total_results
        print '[*] Page 1...'
        pages = max_pages(pages, total_results)
        for r in results['matches']:
            full_ip = '%s:%s' % (r['ip'], r['port'])
            ips_found.append(full_ip)

        if pages > 1:
            i = 2
            while i <= pages:
                results = api.search(search, page=i)
                print '[*] Page %d...' % i
                for r in results['matches']:
                    full_ip = '%s:%s' % (r['ip'], r['port'])
                    ips_found.append(full_ip)
                i += 1

        return ips_found

    except Exception as e:
        print '[!] Shodan search error:', e

def max_pages(pages, total_results):
    ''' Measures the max # of pages in Shodan results. Alternative to this
    would be to measure len(results['matches']) and stop when that is zero,
    but that would mean 1 extra api lookup which would add some pointless
    seconds to the search '''

    total_pages = (total_results+100)/100
    if pages > total_pages:
        pages = total_pages
        return pages
    else:
        return pages

def browser_mechanize():
    ''' Start headless browser '''
    br = mechanize.Browser()
    # Cookie Jar
    cj = cookielib.LWPCookieJar()
    br.set_cookiejar(cj)
    # Browser options
    br.set_handle_equiv(True)
    br.set_handle_gzip(True)
    br.set_handle_redirect(True)
    br.set_handle_referer(True)
    br.set_handle_robots(False)
    # Follows refresh 0 but not hangs on refresh > 0
    br.set_handle_refresh(mechanize._http.HTTPRefreshProcessor(), max_time=1)
    br.addheaders = [('User-agent', 'Mozilla/5.0 (Windows NT 6.3; Trident/7.0; rv:11.0) like Gecko')]
    return br

class Scraper():

    def __init__(self, args):
        self.user = args.username
        self.passwd = args.password
        self.findstring = args.findstring
        self.search = args.shodansearch
        if args.ssl:
            self.uri_prefix = 'https://'
        else:
            self.uri_prefix = 'http://'
        self.targets = args.targets
        self.br = browser_mechanize()

    def run(self, target):
        target = self.uri_prefix+target
        try:
            resp, brtitle = self.req(target)
            title, match = self.html_parser(resp, brtitle)
            if match:
                mark = '+'
                label = match
            else:
                mark = '*'
                label = 'Title:    '
            sublabel = title
        except Exception as e:
            mark = '-'
            label = 'Exception:'
            sublabel = str(e)

        self.final_print(mark, target, label, sublabel)

    def req(self, target):
        ''' Determine what type of auth to use, if any '''
        if self.user and self.passwd:
            # Attempts to login via text boxes
            # Failing that, tries basic auth
            # Failing that, tries no auth
            return self.resp_to_textboxes(target)
        return self.resp_no_auth(target)

    #############################################################################
    # Get response functions
    #############################################################################
    def resp_no_auth(self, target):
        ''' No username/password argument given '''
        no_auth_resp = self.br.open('%s' % target)
        soup = BeautifulSoup(no_auth_resp)
        brtitle = soup.title.text
        return no_auth_resp, brtitle

    def resp_basic_auth(self, target):
        ''' When there are no login forms on page but -u and -p are given'''
        self.br.add_password('%s' % target, self.user, self.passwd)
        basic_auth_resp = self.br.open('%s' % target)
        soup = BeautifulSoup(basic_auth_resp)
        brtitle = soup.title.text
        return basic_auth_resp, brtitle

    def resp_to_textboxes(self, target):
        ''' Find the first form on the page that has exactly 1 text box and 1 password box.
        Fill it out with the credentials the user provides. If no form is found, try
        authenticating with HTTP Basic Auth and if that also fails, try just getting a response. '''
        brtitle1 = None

        try:
            resp = self.br.open('%s' % target)
            soup = BeautifulSoup(resp)
            brtitle1 = soup.title.text
            forms = self.br.forms()
            self.br.form = self.find_password_form(forms)
            resp = self.fill_out_form()
            soup = BeautifulSoup(resp)
            brtitle = soup.title.text
        except Exception:
            # If trying to login via form, try basic auth
            try:
                resp, brtitle = self.resp_basic_auth(target)
            except Exception:
                # If basic auth failed as well, try no auth
                resp, brtitle = self.resp_no_auth(target)

        if brtitle == None and brtitle1:
            brtitle = brtitle1

        return resp, brtitle

    def find_password_form(self, forms):
        for f in forms:
            pw = 0
            text = 0
            for c in f.controls:
                if c.type == 'text':
                    text = text+1
                if c.type == 'password':
                    pw = pw+1
            if pw == 1 and text == 1:
                return f

    def fill_out_form(self):
        ''' Find the first text and password controls and fill them out '''
        text_found = 0
        pw_found = 0
        for c in self.br.form.controls:
            if c.type == 'text':
                # Only get the first text control box
                if text_found == 0:
                    c.value = self.user
                    text_found = 1
                    continue
            if c.type == 'password':
                c.value = self.passwd
                pw_found = 1
                break

        form_resp = self.br.submit()
        return form_resp
    #############################################################################

    def html_parser(self, resp, brtitle):
        ''' Parse html, look for a match with user arg
        and find the title. '''
        html = resp.read()

        # Find match
        match = self.find_match(html)

       # Including lxml in case someone has more success with it
       # My test showed that BeautifulSoup encountered a few less encoding errors (~3% vs 5% from lxml)
       # root = fromstring(html)
       # find_title = root.xpath('//title')
       # try:
       #     title = find_title[0].text
       # except Exception as e:
       #     title = '<None>'

        # Get title
        soup = BeautifulSoup(html)
        title = None
        try:
            title = soup.title.string
        except AttributeError as e:
            if brtitle:
                title == brtitle
        except Exception as e:
            title = str(e)

        if brtitle and not title:
            title = brtitle

        return title, match

    def find_match(self, html):
        match = None
        if self.findstring:
            if self.findstring in html:
                match = '* MATCH * '
        return match

    def final_print(self, mark, target, label, sublabel):
        target = target.ljust(30)

        if self.search:
            name = self.search
        elif self.targets:
            name = self.targets
        else:
            name = None

        name = name.replace('/', '')

        try:
            results = '[%s] %s | %s %s' % (mark, target, label, sublabel)
            if mark == '*' or mark == '+':
                with open('%s_results.txt' % name, 'a+') as f:
                    f.write('[%s] %s | %s %s\n' % (mark, target, label, sublabel))
            print results
        except Exception as e:
            results = '[%s] %s | %s %s' % (mark, target, label, str(e))
            with open('%s_results.txt' % name, 'a+') as f:
                f.write('%s\n' % results)
            print results


#############################################################################
# IP range target handlers
# Taken from against.py by pigtails23 with minor modification
#############################################################################
def get_targets_from_args(targets):
    target_type = check_targets(targets)
    if target_type:
        if target_type == 'domain' or target_type == 'ip':
            return ['%s' % targets]
        elif target_type == 'ip range':
            return ip_range(targets)

def check_targets(targets):
    ''' This could use improvement but works fine would be
    nice to get a good regex just for finding IP ranges '''
    if re.match('^[A-Za-z]', targets): # starts with a letter
        return 'domain'
    elif targets.count('.') == 3 and '-' in targets:
        return 'ip range'
    #if re.match('(?=.*-)', targets):
    #    return 'ip range'
    elif re.match(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$", targets):
        return 'ip'
    else:
        return None

def handle_ip_range(iprange):
    parted = tuple(part for part in iprange.split('.'))
    rsa = range(4)
    rsb = range(4)
    for i in range(4):
        hyphen = parted[i].find('-')
        if hyphen != -1:
            rsa[i] = int(parted[i][:hyphen])
            rsb[i] = int(parted[i][1+hyphen:]) + 1
        else:
            rsa[i] = int(parted[i])
            rsb[i] = int(parted[i]) + 1
    return rsa, rsb

def ip_range(iprange):
    rsa, rsb = handle_ip_range(iprange)
    ips = []
    counter = 0
    for i in range(rsa[0], rsb[0]):
        for j in range(rsa[1], rsb[1]):
            for k in range(rsa[2], rsb[2]):
                for l in range(rsa[3], rsb[3]):
                    ip = '%d.%d.%d.%d' % (i, j, k, l)
                    ips.append(ip)
    return ips
#############################################################################

def main(args):

    S = Scraper(args)

    if not args.targets and not args.shodansearch:
        exit('[!] No targets found. Please use the -s option to specify a search term for Shodan  or specify an IP, IP range, or domain using the -t option')

    if args.targets and args.shodansearch:
        print '[+] Both -s and -t arguments found; defaulting to the targets listed after -t'

    if args.targets:
        targets = get_targets_from_args(args.targets)
    elif args.shodansearch:
        targets = shodan_search(args.shodansearch, args.apikey, int(args.numpages))

    if targets == [] or targets == None:
        exit('[!] No valid targets')

    con = int(args.concurrent)

    # By default run 1000 concurrently at a time
    target_groups = [targets[x:x+con] for x in xrange(0, len(targets), con)]
    for chunk_targets in target_groups:
        jobs = [gevent.spawn(S.run, target) for target in chunk_targets]
        gevent.joinall(jobs)

if __name__ == "__main__":
    main(parse_args())
