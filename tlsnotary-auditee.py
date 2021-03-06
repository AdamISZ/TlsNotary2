#!/usr/bin/env python
from __future__ import print_function

from base64 import b64decode, b64encode
import BaseHTTPServer
import binascii
import codecs
from hashlib import md5, sha1, sha256
import hmac
import os
from os.path import join
import platform
import Queue
import random
import re
import select
import shutil
import signal
import SimpleHTTPServer
import socket
from subprocess import Popen, check_output
import sys
import tarfile
import threading
import time
import zipfile
try: import wingdbstub
except: pass

installdir = os.path.dirname(os.path.realpath(__file__))
datadir = join(installdir, 'data')
sessionsdir = join(datadir, 'sessions')
time_str = time.strftime('%d-%b-%Y-%H-%M-%S', time.gmtime())
current_sessiondir = join(sessionsdir, time_str)
nss_patch_dir = join(current_sessiondir, 'nsspatchdir')
os.makedirs(nss_patch_dir)

m_platform = platform.system()
if m_platform == 'Windows': OS = 'mswin'
elif m_platform == 'Linux': OS = 'linux'
elif m_platform == 'Darwin': OS = 'macos'
 
IRCsocket = None
recvQueue = Queue.Queue() #all messages from the auditor are placed here by receivingThread
ackQueue = Queue.Queue() #ack numbers are placed here
auditor_nick = '' #we learn auditor's nick as soon as we get a hello_server signed by the auditor
my_nick = '' #our nick is randomly generated on connection to IRC
channel_name = '#tlsnotary'
myPrvKey = auditorPubKey = None
google_modulus = 0
google_exponent = 0

stcppipe_proc = None
tshark_exepath = ''
bReceivingThreadStopFlagIsSet = False
secretbytes_amount=13
firefox_pid = stcppipe_pid = selftest_pid = 0

PMS1 = '' #first half of pre-master secret. global because of google check
md5hmac = '' #used in get_html_paths to construct the full MS after committing to a hash
bIsStcppipeStarted = False
cr_list = [] #a list of all client_randoms for recorded pages used by tshark to search for html only in audited tracefiles.

def bigint_to_bytearray(bigint):
    m_bytes = []
    while bigint != 0:
        b = bigint%256
        m_bytes.insert( 0, b )
        bigint //= 256
    return bytearray(m_bytes)

def bigint_to_list(bigint):
    m_bytes = []
    while bigint != 0:
        b = bigint%256
        m_bytes.insert( 0, b )
        bigint //= 256
    return m_bytes


def xor(a,b):
    return bytearray([ord(a) ^ ord(b) for a,b in zip(a,b)])

#convert bytearray into int
def ba2int(byte_array):
    return int(str(byte_array).encode('hex'), 16)

#a thread which returns a value. This is achieved by passing self as the first argument to a target function
#the target_function(parentthread, arg1, arg2) can then set, e.g parentthread.retval
class ThreadWithRetval(threading.Thread):
    def __init__(self, target, args=()):
        super(ThreadWithRetval, self).__init__(target=target, args = (self,)+args )
    retval = ''


def import_auditor_pubkey(auditor_pubkey_b64modulus):
    global auditorPubKey                      
    try:
        auditor_pubkey_modulus = b64decode(auditor_pubkey_b64modulus)
        auditor_pubkey_modulus_int =  ba2int(auditor_pubkey_modulus)
        auditorPubKey = rsa.PublicKey(auditor_pubkey_modulus_int, 65537)
        auditor_pubkey_pem = auditorPubKey.save_pkcs1()
        with open(join(current_sessiondir, 'auditorpubkey'), 'wb') as f: f.write(auditor_pubkey_pem)
        #also save the key as recent, so that they could be reused in the next session
        if not os.path.exists(join(datadir, 'recentkeys')): os.makedirs(join(datadir, 'recentkeys'))
        with open(join(datadir, 'recentkeys' , 'auditorpubkey'), 'wb') as f: f.write(auditor_pubkey_pem)
        return ('success')
    except Exception,e:
        print (e)
        return ('failure')


def newkeys():
    global myPrvKey                
    #Usually the auditee would reuse a keypair from the previous session
    #but for privacy reasons the auditee may want to generate a new key
    pubkey, privkey = rsa.newkeys(1024)
    myPrvKey = privkey
    my_pem_pubkey = pubkey.save_pkcs1()
    my_pem_privkey = privkey.save_pkcs1()
    with open(join(current_sessiondir, 'myprivkey'), 'wb') as f: f.write(my_pem_privkey)
    with open(join(current_sessiondir, 'mypubkey'), 'wb') as f: f.write(my_pem_pubkey)
    #also save the keys as recent, so that they could be reused in the next session
    if not os.path.exists(join(datadir, 'recentkeys')): os.makedirs(join(datadir, 'recentkeys'))
    with open(join(datadir, 'recentkeys', 'myprivkey'), 'wb') as f: f.write(my_pem_privkey)
    with open(join(datadir, 'recentkeys', 'mypubkey'), 'wb') as f: f.write(my_pem_pubkey)
    pubkey_export = b64encode(bigint_to_bytearray(pubkey.n))
    return pubkey_export


#Receive HTTP HEAD requests from FF addon
class HandlerClass(SimpleHTTPServer.SimpleHTTPRequestHandler):
    #HTTP/1.0 instead of HTTP/1.1 is crucial, otherwise the http server just keep hanging
    #https://mail.python.org/pipermail/python-list/2013-April/645128.html
    protocol_version = 'HTTP/1.0'      
    
    def respond(self, headers):
        # we need to adhere to CORS and add extra Access-Control-* headers in server replies                
        keys = [k for k in headers]
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Expose-Headers', ','.join(keys))
        for key in headers:
            self.send_header(key, headers[key])
        self.end_headers()        
    
    def do_HEAD(self):      
        print ('minihttp received ' + self.path + ' request',end='\r\n')
        # example HEAD string "/command?parameter=124value1&para2=123value2"    
        if self.path.startswith('/get_recent_keys'):
            #the very first command from addon 
            #on tlsnotary frst run, there will be no saved keys
            #otherwise we load up the keys saved from previous session
            my_prvkey_pem = my_pubkey_pem = auditor_pubkey_pem = ''
            if os.path.exists(join(datadir, 'recentkeys')):
                if os.path.exists(join(datadir, 'recentkeys', 'myprivkey')) and os.path.exists(join(datadir, 'recentkeys', 'mypubkey')):
                    with open(join(datadir, 'recentkeys', 'myprivkey'), 'rb') as f: my_prvkey_pem = f.read()
                    with open(join(datadir, 'recentkeys', 'mypubkey'), 'rb') as f: my_pubkey_pem = f.read()
                    with open(join(current_sessiondir, 'myprivkey'), 'wb') as f: f.write(my_prvkey_pem)
                    with open(join(current_sessiondir, 'mypubkey'), 'wb') as f: f.write(my_pubkey_pem)
                    global myPrvKey                    
                    myPrvKey = rsa.PrivateKey.load_pkcs1(my_prvkey_pem)
                if os.path.exists(join(datadir, 'recentkeys', 'auditorpubkey')):
                    with open(join(datadir, 'recentkeys', 'auditorpubkey'), 'rb') as f: auditor_pubkey_pem = f.read()
                    with open(join(current_sessiondir, 'auditorpubkey'), 'wb') as f: f.write(auditor_pubkey_pem)
                    global auditorPubKey                    
                    auditorPubKey = rsa.PublicKey.load_pkcs1(auditor_pubkey_pem)
                my_pubkey = rsa.PublicKey.load_pkcs1(my_pubkey_pem)
                my_pubkey_export = b64encode(bigint_to_bytearray(my_pubkey.n))
                if auditor_pubkey_pem == '': auditor_pubkey_export = ''
                else: auditor_pubkey_export = b64encode(bigint_to_bytearray(auditorPubKey.n))
                self.respond({'response':'get_recent_keys', 'mypubkey':my_pubkey_export,
                         'auditorpubkey':auditor_pubkey_export})
            else:
                self.respond({'response':'get_recent_keys', 'mypubkey':'', 'auditorpubkey':''})                
            return            
        #---------------------------------------------------------------------#     
        if self.path.startswith('/new_keypair'):
            pubkey_export = newkeys()
            self.respond({'response':'new_keypair', 'pubkey':pubkey_export,
                                 'status':'success'})
            return        
        #----------------------------------------------------------------------#
        if self.path.startswith('/import_auditor_pubkey'):
            arg_str = self.path.split('?', 1)[1]
            if not arg_str.startswith('pubkey='):
                self.respond({'response':'import_auditor_pubkey', 'status':'wrong HEAD parameter'})
                return
            #else
            auditor_pubkey_b64modulus = arg_str[len('pubkey='):]            
            status = import_auditor_pubkey(auditor_pubkey_b64modulus)           
            self.respond({'response':'import_auditor_pubkey', 'status':status})
            return        
        #----------------------------------------------------------------------#
        if self.path.startswith('/start_irc'):
            rv = start_irc()
            self.respond({'response':'start_irc', 'status':rv})
            return       
        #----------------------------------------------------------------------#
        if self.path.startswith('/start_recording'):
            global bIsStcppipeStarted            
            if not bIsStcppipeStarted:
                bIsStcppipeStarted = True
                rv = start_recording()
            else:
                rv = ('success', 'success')
            if rv[0] != 'success':
                self.respond({'response':'start_recording', 'status':rv[0]})
                return
            else:
                self.respond({'response':'start_recording', 'status':rv[0], 'proxy_port':rv[1]})
                return        
        #----------------------------------------------------------------------#
        if self.path.startswith('/stop_recording'):
            rv = stop_recording()
            self.respond({'response':'stop_recording', 'status':rv,
                          'session_path':join(current_sessiondir, 'mytrace')})
            return      
        #----------------------------------------------------------------------#
        if self.path.startswith('/prepare_pms'):
            rv = prepare_pms()
            if rv[0] == 'success':
                global PMS1
                PMS1 = rv[1]
            self.respond({'response':'prepare_pms', 'status':rv[0]})
            return             
        #----------------------------------------------------------------------#
        if self.path.startswith('/send_link'):
            filelink = self.path.split('?', 1)[1]
            rv = send_link(filelink)
            self.respond({'response':'send_link', 'status':rv})
            return      
        #----------------------------------------------------------------------#
        if self.path.startswith('/get_html_paths'):
            rv = get_html_paths()
            b64_paths = ''
            if rv[0] == 'success': b64_paths = b64encode(rv[1])
            status = 'success' if rv[0] == 'success' else rv[1]
            self.respond({'response':'get_html_paths', 'status':status, 'html_paths':b64_paths})
            return                  
        #----------------------------------------------------------------------#
        if self.path.startswith('/selftest'):
            output = check_output([sys.executable, join(installdir, 'tlsnotary-auditor.py'), 'daemon', 'genkey'])
            auditor_key = output.split()[-1]
            import_auditor_pubkey(auditor_key)
            print ('Imported auditor key')
            print (auditor_key)
            my_newkey = newkeys()
            proc = Popen([sys.executable, join(installdir, 'tlsnotary-auditor.py'), 'daemon', 'hiskey='+my_newkey])
            global selftest_pid
            selftest_pid = proc.pid
            self.respond({'response':'selftest', 'status':'success'})
            return            
        #----------------------------------------------------------------------#
        else:
            self.respond({'response':'unknown command'})
            return

def get_html_paths():
    cr = cr_list[-1]
    #find tracefile containing cr and commit to its hash as well as the hash of md5hmac (for MS)
    #Construct MS and decrypt HTML files to be presented to auditee for approval
    tracelog_dir = join(current_sessiondir, 'tracelog')
    tracelog_files = os.listdir(tracelog_dir)
    bFoundCR = False
    for one_trace in tracelog_files:
        with open(join(tracelog_dir, one_trace), 'rb') as f: data=f.read()
        if not data.count(cr) == 1: continue
        #else client random found
        bFoundCR = True
        break 
    if not bFoundCR: raise Exception ('Client random not found in trace files')
    #copy the tracefile to a new location, b/c stcppipe may still be appending it 
    commit_dir = join(current_sessiondir, 'commit')
    if not os.path.exists(commit_dir): os.makedirs(commit_dir)
    tracecopy_path = join(commit_dir, 'trace'+ str(len(cr_list)) )
    md5hmac_path = join(commit_dir, 'md5hmac'+ str(len(cr_list)) )
    with open(md5hmac_path, 'wb') as f: f.write(md5hmac)
    shutil.copyfile(join(tracelog_dir, one_trace), tracecopy_path)
    #send the hash of tracefile and md5hmac
    with open(tracecopy_path, 'rb') as f: data=f.read()
    commit_hash = sha256(data).digest()
    md5hmac_hash = sha256(md5hmac).digest()  
    b64_commit_hash = b64encode(commit_hash+md5hmac_hash)
    reply = send_and_recv('commit_hash:'+b64_commit_hash)
    if reply[0] != 'success': raise Exception ('Failed to receive a reply')
    if not reply[1].startswith('sha1hmac_for_MS:'):
        raise Exception ('bad reply. Expected sha1hmac_for_MS')
    b64_sha1hmac_for_MS = reply[1][len('sha1hmac_for_MS:'):]
    try: sha1hmac_for_MS = b64decode(b64_sha1hmac_for_MS)
    except:  raise Exception ('base64 decode error in sha1hmac_for_MS')
    #construct MS
    ms = xor(md5hmac, sha1hmac_for_MS)[:48]
    sslkeylog = join(commit_dir, 'sslkeylog' + str(len(cr_list)))
    ssldebuglog = join(commit_dir, 'ssldebuglog' + str(len(cr_list)))    
    cr_hexl = binascii.hexlify(cr)
    ms_hexl = binascii.hexlify(ms)
    skl_fd = open(sslkeylog, 'wb')
    skl_fd.write('CLIENT_RANDOM ' + cr_hexl + ' ' + ms_hexl + '\n')
    skl_fd.close()
    #use tshark to extract HTML
    try: output = check_output([tshark_exepath, '-r', tracecopy_path,
                                          '-Y', 'ssl and http.content_type contains html',
                                          '-o', 'http.ssl.port:1025-65535', 
                                          '-o', 'ssl.keylog_file:'+ sslkeylog,
                                          '-o', 'ssl.ignore_ssl_mac_failed:False',
                                          '-o', 'ssl.debug_file:' + ssldebuglog,
                                          '-x'])
    except: #maybe this is an old tshark version, change -Y to -R
        try: output = check_output([tshark_exepath, '-r', tracecopy_path,
                                              '-R', 'ssl and http.content_type contains html', 
                                               '-o', 'http.ssl.port:1025-65535', 
                                               '-o', 'ssl.keylog_file:'+ sslkeylog,
                                               '-o', 'ssl.ignore_ssl_mac_failed:False',
                                               '-o', 'ssl.debug_file:' + ssldebuglog,
                                               '-x'])
        except: raise Exception('Failed to launch tshark')
    if output == '': return ('failure', 'Failed to find HTML in escrowtrace')
    with open(ssldebuglog, 'rb') as f: debugdata = f.read()
    if debugdata.count('mac failed') > 0: raise Exception('Mac check failed in tracefile')
    #output may contain multiple frames with HTML, we examine them one-by-one
    separator = re.compile('Frame ' + re.escape('(') + '[0-9]{2,7} bytes' + re.escape(')') + ':')
    #ignore the first split element which is always an empty string
    frames = re.split(separator, output)[1:]    
    html_paths = ''
    for index,oneframe in enumerate(frames):
        html = get_html_from_asciidump(oneframe)
        path = join(commit_dir, 'html-' + str(len(cr_list)) + '-' + str(index))
        with open(path, 'wb') as f: f.write(html)
        html_paths += path + '&'    
    return ('success', html_paths)
    
    
#look at tshark's ascii dump (option '-x') to better understand the parsing taking place
def get_html_from_asciidump(ascii_dump):
    hexdigits = set('0123456789abcdefABCDEF')
    binary_html = bytearray()

    if ascii_dump == '':
        print ('empty frame dump',end='\r\n')
        return -1

    #We are interested in
    # "Uncompressed entity body" for compressed HTML (both chunked and not chunked). If not present, then
    # "De-chunked entity body" for no-compression, chunked HTML. If not present, then
    # "Reassembled SSL" for no-compression no-chunks HTML in multiple SSL segments, If not present, then
    # "Decrypted SSL data" for no-compression no-chunks HTML in a single SSL segment.
    
    uncompr_pos = ascii_dump.rfind('Uncompressed entity body')
    if uncompr_pos != -1:
        for line in ascii_dump[uncompr_pos:].split('\n')[1:]:
            #convert ascii representation of hex into binary so long as first 4 chars are hexdigits
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #if first 4 chars are not hexdigits, we reached the end of the section
                break
        return binary_html
    
    #else      
    dechunked_pos = ascii_dump.rfind('De-chunked entity body')
    if dechunked_pos != -1:
        for line in ascii_dump[dechunked_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                break
        return binary_html
            
    #else
    reassembled_pos = ascii_dump.rfind('Reassembled SSL')
    if reassembled_pos != -1:
        for line in ascii_dump[reassembled_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #http HEADER is delimited from HTTP body with '\r\n\r\n'
                if binary_html.find('\r\n\r\n') == -1:
                    return -1
                break
        return binary_html.split('\r\n\r\n', 1)[1]

    #else
    decrypted_pos = ascii_dump.rfind('Decrypted SSL data')
    if decrypted_pos != -1:       
        for line in ascii_dump[decrypted_pos:].split('\n')[1:]:
            if all(c in hexdigits for c in line [:4]):
                try: m_array = bytearray.fromhex(line[6:54])
                except: break
                binary_html += m_array
            else:
                #http HEADER is delimited from HTTP body with '\r\n\r\n'
                if binary_html.find('\r\n\r\n') == -1:
                    return -1
                break
        return binary_html.split('\r\n\r\n', 1)[1]    
    

def send_link(filelink):
    b64_link = b64encode(filelink)
    reply = send_and_recv('link:'+b64_link)
    if not reply[0] == 'success' : return 'failure'
    if not reply[1].startswith('response:') : return 'failure'
    response = reply[1][len('response:'):]
    return response


#Because there is a 1 in 6 chance that the encrypted PMS will contain zero bytes in it's
#padding, we first try the encrypted PMS with google.com and see if it gets rejected.
#return my first half of PMS which will be used in the actual audited connection to the server
def prepare_pms():    
    for i in range(5): #try 5 times until google check succeeds
        #first 4 bytes of client random are unix time
        cr_time = bigint_to_bytearray(int(time.time()))
        cr = cr_time + os.urandom(28)
        client_hello = '\x16\x03\x01\x00\x2d\x01\x00\x00\x29\x03\x01' + cr + '\x00\x00\x02\x00\x35\x01\x00'
        tlssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tlssock.settimeout(10)
        tlssock.connect(('google.com', 443))
        tlssock.send(client_hello)
        #we must get 3 concatenated tls handshake messages in response:
        #sh --> server_hello, cert --> certificate, shd --> server_hello_done
        time.sleep(1)
        sh_cert_shd = tlssock.recv(8192*2)  #google sends a ridiculously long cert chain of 10KB+
        #server hello always starts with 16 03 01 * * 02
        #certificate always starts with 16 03 01 * * 0b
        shd = '\x16\x03\x01\x00\x04\x0e\x00\x00\x00'
        sh_magic = re.compile(b'\x16\x03\x01..\x02')
        if not re.match(sh_magic, sh_cert_shd): raise Exception ('Invalid server hello')
        if not sh_cert_shd.endswith(shd): raise Exception ('invalid server hello done')
        #find the beginning of certificate message
        cert_magic = re.compile(b'\x16\x03\x01..\x0b')
        cert_match = re.search(cert_magic, sh_cert_shd)
        if not cert_match: raise Exception ('Invalid certificate message')
        cert_start_position = cert_match.start()
        sh = sh_cert_shd[:cert_start_position]
        cert = sh_cert_shd[cert_start_position : -len(shd)]
        #extract google_server_random from server_hello
        sr = sh[11:43]
        #give auditor cr&sr and get an encrypted second half of PMS,
        #and shahmac that needs to be xored with my md5hmac to get MS
        b64_crsr = b64encode(cr+sr)
        reply = send_and_recv('gcr_gsr:'+b64_crsr)       
        if reply[0] != 'success': raise Exception ('Failed to receive a reply for gcr_gsr:')
        if not reply[1].startswith('grsapms_ghmac:'): raise Exception ('bad reply. Expected grsapms_ghmac:')    
        b64_grsapms_ghmac = reply[1][len('grsapms_ghmac:'):]
        try: grsapms_ghmac = b64decode(b64_grsapms_ghmac)    
        except: raise Exception ('base64 decode error in grsapms_ghmac')       
        rsapms2 = grsapms_ghmac[:256]
        shahmac = grsapms_ghmac[256:304]
        #generate my first half of PMS which will be returned if the check with 
        #google.com is successful
        pms1 = '\x03\x01'+os.urandom(secretbytes_amount) + ('\x00' * (24-2-secretbytes_amount))
        #derive MS
        label = 'master secret'
        seed = cr + sr       
        md5A1 = hmac.new(pms1, label+seed, md5).digest()
        md5A2 = hmac.new(pms1, md5A1, md5).digest()
        md5A3 = hmac.new(pms1, md5A2, md5).digest()      
        md5hmac1 = hmac.new(pms1, md5A1 + label + seed, md5).digest()
        md5hmac2 = hmac.new(pms1, md5A2 + label + seed, md5).digest()
        md5hmac3 = hmac.new(pms1, md5A3 + label + seed, md5).digest()
        md5hmac = md5hmac1+md5hmac2+md5hmac3
        ms = xor(md5hmac, shahmac)[:48]
        #derive expanded keys for AES256
        #this is not optimized in a loop on purpose. I want people to see exactly what is going on        
        ms_first_half = ms[:24]
        ms_second_half = ms[24:]
        label = 'key expansion'
        seed = sr + cr
        md5A1 = hmac.new(ms_first_half, label+seed, md5).digest()
        md5A2 = hmac.new(ms_first_half, md5A1, md5).digest()
        md5A3 = hmac.new(ms_first_half, md5A2, md5).digest()
        md5A4 = hmac.new(ms_first_half, md5A3, md5).digest()
        md5A5 = hmac.new(ms_first_half, md5A4, md5).digest()
        md5A6 = hmac.new(ms_first_half, md5A5, md5).digest()
        md5A7 = hmac.new(ms_first_half, md5A6, md5).digest()
        md5A8 = hmac.new(ms_first_half, md5A7, md5).digest()
        md5A9 = hmac.new(ms_first_half, md5A8, md5).digest()
        #---------#
        md5hmac1 = hmac.new(ms_first_half, md5A1 + label + seed, md5).digest()
        md5hmac2 = hmac.new(ms_first_half, md5A2 + label + seed, md5).digest()
        md5hmac3 = hmac.new(ms_first_half, md5A3 + label + seed, md5).digest()
        md5hmac4 = hmac.new(ms_first_half, md5A4 + label + seed, md5).digest()
        md5hmac5 = hmac.new(ms_first_half, md5A5 + label + seed, md5).digest()
        md5hmac6 = hmac.new(ms_first_half, md5A6 + label + seed, md5).digest()
        md5hmac7 = hmac.new(ms_first_half, md5A7 + label + seed, md5).digest()
        md5hmac8 = hmac.new(ms_first_half, md5A8 + label + seed, md5).digest()
        md5hmac9 = hmac.new(ms_first_half, md5A9 + label + seed, md5).digest()       
        md5hmac = md5hmac1+md5hmac2+md5hmac3+md5hmac4+md5hmac5+md5hmac6+md5hmac7+md5hmac8+md5hmac9
        #---------#    
        sha1A1 = hmac.new(ms_second_half, label+seed, sha1).digest()
        sha1A2 = hmac.new(ms_second_half, sha1A1, sha1).digest()
        sha1A3 = hmac.new(ms_second_half, sha1A2, sha1).digest()
        sha1A4 = hmac.new(ms_second_half, sha1A3, sha1).digest()
        sha1A5 = hmac.new(ms_second_half, sha1A4, sha1).digest()
        sha1A6 = hmac.new(ms_second_half, sha1A5, sha1).digest()
        sha1A7 = hmac.new(ms_second_half, sha1A6, sha1).digest()
        #---------#        
        sha1hmac1 = hmac.new(ms_second_half, sha1A1 + label + seed, sha1).digest()
        sha1hmac2 = hmac.new(ms_second_half, sha1A2 + label + seed, sha1).digest()
        sha1hmac3 = hmac.new(ms_second_half, sha1A3 + label + seed, sha1).digest()
        sha1hmac4 = hmac.new(ms_second_half, sha1A4 + label + seed, sha1).digest()
        sha1hmac5 = hmac.new(ms_second_half, sha1A5 + label + seed, sha1).digest()
        sha1hmac6 = hmac.new(ms_second_half, sha1A6 + label + seed, sha1).digest()
        sha1hmac7 = hmac.new(ms_second_half, sha1A7 + label + seed, sha1).digest()        
        sha1hmac = sha1hmac1+sha1hmac2+sha1hmac3+sha1hmac4+sha1hmac5+sha1hmac6+sha1hmac7        
        gexpanded_keys = xor(md5hmac, sha1hmac)
        client_mac_key = gexpanded_keys[:20]
        client_encryption_key = gexpanded_keys[40:72]
        client_iv = gexpanded_keys[104:120]
        #RSA-encrypt my half of PMS with google's pubkey
        RSA_PMS1_int = pow( ba2int('\x02'+('\x01'*156)+'\x00'+pms1+('\x00'*24)) + 1, google_exponent, google_modulus)
        RSA_PMS2_int = ba2int(rsapms2)
        enc_pms_int = (RSA_PMS1_int*RSA_PMS2_int) % google_modulus 
        encpms = bigint_to_bytearray(enc_pms_int)    
        #calculate verify_data for Finished message
        #see RFC2246 7.4.9. Finished & 5. HMAC and the pseudorandom function
        client_key_exchange = '\x16\x03\x01\x01\x06\x10\x00\x01\x02\x01\00' + encpms        
        handshake_messages = client_hello[5:]+sh[5:]+cert[5:]+shd[5:]+client_key_exchange[5:]
        sha_verify = sha1(handshake_messages).digest()
        md5_verify = md5(handshake_messages).digest()
        label = 'client finished'
        seed = md5_verify + sha_verify
        ms_first_half = ms[:24]
        ms_second_half = ms[24:]       
        md5A1 = hmac.new(ms_first_half, label+seed, md5).digest()
        md5hmac1 = hmac.new(ms_first_half, md5A1 + label + seed, md5).digest()        
        sha1A1 = hmac.new(ms_second_half, label+seed, sha1).digest()
        sha1hmac1 = hmac.new(ms_second_half, sha1A1 + label + seed, sha1).digest()
        verify_data = xor(md5hmac1, sha1hmac1)[:12]
        #HMAC and AES-encrypt the verify_data      
        hmac_for_verify_data = hmac.new(client_mac_key, '\x00\x00\x00\x00\x00\x00\x00\x00' + '\x16' + '\x03\x01' + '\x00\x10' + '\x14\x00\x00\x0c' + verify_data, sha1).digest()
        moo = AESModeOfOperation()
        cleartext = '\x14\x00\x00\x0c' + verify_data + hmac_for_verify_data     
        cleartext_list = bigint_to_list(ba2int(cleartext))
        client_encryption_key_list =  bigint_to_list(ba2int(client_encryption_key))
        client_iv_list =  bigint_to_list(ba2int(client_iv))
        padded_cleartext = cleartext + ('\x0b' * 12) #this is TLS CBC padding, NOT PKCS7
        try:
            mode, orig_len, encrypted_verify_data_and_hmac_for_verify_data = moo.encrypt( str(padded_cleartext), moo.modeOfOperation['CBC'], client_encryption_key_list, moo.aes.keySize['SIZE_256'], client_iv_list)
        except Exception, e: # TODO find out why I once got TypeError: 'NoneType' object is not iterable.  It helps to catch an exception here
            print ('Caught exception while doing slowaes encrypt: ', e)
            tlssock.close()
            continue
        #send and expect change cipher spec from google.com as a sign of success
        change_cipher_spec = '\x14\x03\01\x00\x01\x01'
        finished = '\x16\x03\x01\x00\x30' + bytearray(encrypted_verify_data_and_hmac_for_verify_data)       
        tlssock.send(client_key_exchange+change_cipher_spec+finished)
        time.sleep(1)
        response = tlssock.recv(8192)
        if not response.count(change_cipher_spec):
            #the response did not contain ccs == error alert received
            tlssock.close()
            continue
        #else ccs was in the response
        tlssock.close()
        return ('success', pms1) #successfull pms check        
    #no dice after 5 tries
    raise Exception ('Could not prepare PMS with google after 5 tries')

    
#send a message and return the response received
def send_and_recv (data):
    if not hasattr(send_and_recv, 'my_seq'):
        send_and_recv.my_seq = 0 #static variable. Initialized only on first function's run
  
    #empty queue from possible leftovers
    #try: ackQueue.get_nowait()
    #except: pass    #split up data longer than chunk_size bytes (IRC message limit is 512 bytes including the header data)
    #'\r\n' must go to the end of each message
    chunk_size=350
    chunks = len(data)/chunk_size + 1
    if len(data)%chunk_size == 0: chunks -= 1 #avoid creating an empty chunk if data length is a multiple of chunk_size
    for chunk_index in range(chunks) :
        send_and_recv.my_seq += 1
        chunk = data[chunk_size*chunk_index:chunk_size*(chunk_index+1)]
        
        for i in range (3):
            bWasMessageAcked = False
            ending = ' EOL ' if chunk_index+1==chunks else ' CRLF ' #EOL for the last chunk, otherwise CRLF
            irc_msg = 'PRIVMSG ' + channel_name + ' :' + auditor_nick + ' seq:' + str(send_and_recv.my_seq) + ' ' + chunk + ending +'\r\n'
            bytessent = IRCsocket.send(irc_msg)
            print('SENT:' + str(bytessent) + ' ' +  irc_msg)
        
            try: oneAck = ackQueue.get(block=True, timeout=5)
            except:  continue #send again because ack was not received
            if not str(send_and_recv.my_seq) == oneAck: continue
            #else: correct ack received
            bWasMessageAcked = True
            break
        if not bWasMessageAcked:
            return ('failure', '')
    
    #receive a response
    for i in range(3):
        try: onemsg = recvQueue.get(block=True, timeout=5)
        except:  continue #try to receive again
        return ('success', onemsg)
    return ('failure', '')

def sendspace_getlink(mfile):
    reply = requests.get('https://www.sendspace.com/', timeout=5)
    url_start = reply.text.find('<form method="post" action="https://') + len('<form method="post" action="')
    url_len = reply.text[url_start:].find('"')
    url = reply.text[url_start:url_start+url_len]
    
    sig_start = reply.text.find('name="signature" value="') + len('name="signature" value="')
    sig_len = reply.text[sig_start:].find('"')
    sig = reply.text[sig_start:sig_start+sig_len]
    
    progr_start = reply.text.find('name="PROGRESS_URL" value="') + len('name="PROGRESS_URL" value="')
    progr_len = reply.text[progr_start:].find('"')
    progr = reply.text[progr_start:progr_start+progr_len]
    
    r=requests.post(url, files={'upload_file[]': open(mfile, 'rb')}, data={
        'signature':sig, 'PROGRESS_URL':progr, 'js_enabled':'0', 
        'upload_files':'', 'terms':'1', 'file[]':'', 'description[]':'',
        'recpemail_fcbkinput':'recipient@email.com', 'ownemail':'', 'recpemail':''}, timeout=5)
    
    link_start = r.text.find('"share link">') + len('"share link">')
    link_len = r.text[link_start:].find('</a>')
    link = r.text[link_start:link_start+link_len]
    
    dl_req = requests.get(link)
    dl_start = dl_req.text.find('"download_button" href="') + len('"download_button" href="')
    dl_len = dl_req.text[dl_start:].find('"')
    dl_link = dl_req.text[dl_start:dl_start+dl_len]
    return dl_link


def pipebytes_post(key, mfile):
    #the server responds only when the recepient picks up the file
    requests.post('http://host03.pipebytes.com/put.py?key='+key+'&r='+
                  ('%.16f' % random.uniform(0,1)), files={'file': open(mfile, 'rb')})    


def pipebytes_getlink(mfile):
    reply1 = requests.get('http://host03.pipebytes.com/getkey.php?r='+
                          ('%.16f' % random.uniform(0,1)), timeout=5)
    key = reply1.text
    reply2 = requests.post('http://host03.pipebytes.com/setmessage.php?r='+
                           ('%.16f' % random.uniform(0,1))+'&key='+key, {'message':''}, timeout=5)
    thread = threading.Thread(target= pipebytes_post, args=(key, mfile))
    thread.daemon = True
    thread.start()
    time.sleep(1)               
    reply4 = requests.get('http://host03.pipebytes.com/status.py?key='+key+
                          '&touch=yes&r='+('%.16f' % random.uniform(0,1)), timeout=5)
    return ('http://host03.pipebytes.com/get.py?key='+key)


def stop_recording():
    global bReceivingThreadStopFlagIsSet
    os.kill(stcppipe_proc.pid, signal.SIGTERM)
    #TODO stop https proxy. 

    #trace* files in committed dir is what auditor needs
    tracedir = join(current_sessiondir, 'mytrace')
    os.makedirs(tracedir)
    zipf = zipfile.ZipFile(join(tracedir, 'mytrace.zip'), 'w')
    commit_dir = join(current_sessiondir, 'commit')
    com_dir_files = os.listdir(commit_dir)
    for onefile in com_dir_files:
        if not onefile.startswith(('trace', 'md5hmac')): continue
        zipf.write(join(commit_dir, onefile), onefile)
    zipf.close()
    try: link = sendspace_getlink(join(tracedir, 'mytrace.zip'))
    except:
        try: link = pipebytes_getlink(join(tracedir, 'mytrace.zip'))
        except: return 'failure'
    rv = send_link(link)
    if rv != 'failure':
        return 'success'
    else: return 'failure'

    
#The NSS patch has created a new file in the nss_patch_dir
def new_audited_connection(uid): 
    global md5hmac
    
    with  open(join(nss_patch_dir, 'der'+uid), 'rb') as fd: der = fd.read()
    #TODO: find out why on windows \r\n newline makes its way into der encoding
    if OS=='mswin': der = der.replace('\r\n', '\n')
    with  open(join(nss_patch_dir, 'cr'+uid), 'rb') as fd: cr = fd.read()
    with open(join(nss_patch_dir, 'sr'+uid), 'rb') as fd: sr = fd.read()
    with open(join(nss_patch_dir, 'cipher_suite'+uid), 'rb') as fd: cs = fd.read()
    #cipher suite 2 bytes long in network byte order, we need only the first byte
    cipher_suite_first_byte = cs[:1]
    cipher_suite_int = ba2int(cipher_suite_first_byte)
    if cipher_suite_int == 4: cipher_suite = 'RC4MD5'
    elif cipher_suite_int == 5: cipher_suite = 'RC4SHA'
    elif cipher_suite_int == 47: cipher_suite = 'AES128'
    elif cipher_suite_int == 53: cipher_suite = 'AES256'
    else: raise Exception ('invalid cipher sute')
    cr_list.append(cr)   
    #extract n and e from the pubkey
    try:       
        rv  = decoder.decode(der, asn1Spec=univ.Sequence())
        bitstring = rv[0].getComponentByPosition(1)
        #bitstring is a list of ints, like [01110001010101000...]
        #convert it into into a string   '01110001010101000...'
        stringOfBits = ''
        for bit in bitstring:
            bit_as_str = str(bit)
            stringOfBits += bit_as_str    
        #treat every 8 chars as an int and pack the ints into a bytearray
        ba = bytearray()
        for i in range(0, len(stringOfBits)/8):
            onebyte = stringOfBits[i*8 : (i+1)*8]
            oneint = int(onebyte, base=2)
            ba.append(oneint)  
        #decoding the nested sequence
        rv  = decoder.decode(str(ba), asn1Spec=univ.Sequence())
        exponent = rv[0].getComponentByPosition(1)
        modulus = rv[0].getComponentByPosition(0)
        modulus_int = int(modulus)
        exponent_int = int(exponent)
        n = bigint_to_bytearray(modulus_int)
        e = bigint_to_bytearray(exponent_int)
    except: return 'Error decoding der pubkey'
    modulus_len_int = len(n)       
    modulus_len = bigint_to_bytearray(modulus_len_int)
    if len(modulus_len) == 1: modulus_len.insert(0,0)  #zero-pad to 2 bytes    
    #get my md5hmac half which auditor uses to get his half of MS
    label = 'master secret'
    seed = cr + sr    
    md5A1 = hmac.new(PMS1, label+seed, md5).digest()
    md5A2 = hmac.new(PMS1, md5A1, md5).digest()
    md5A3 = hmac.new(PMS1, md5A2, md5).digest()
    md5hmac1 = hmac.new(PMS1, md5A1 + label + seed, md5).digest()
    md5hmac2 = hmac.new(PMS1, md5A2 + label + seed, md5).digest()
    md5hmac3 = hmac.new(PMS1, md5A3 + label + seed, md5).digest()
    md5hmac = md5hmac1+md5hmac2+md5hmac3
    md5hmac1_for_MS = md5hmac[:24]
    md5hmac2_for_MS = md5hmac[24:48]
          
    b64_cr_sr_hmac_n_e= b64encode(cipher_suite_first_byte+cr+sr+
                                             md5hmac1_for_MS+modulus_len+n+e)
    reply = send_and_recv('cr_sr_hmac_n_e:'+b64_cr_sr_hmac_n_e)    
    if reply[0] != 'success': return ('Failed to receive a reply for cr_sr_hmac_n_e:')
    if not reply[1].startswith('rsapms_hmacms_hmacek:'):
        return 'bad reply. Expected rsapms_hmacms_hmacek_grsapms_ghmac:'
    b64_rsapms_hmacms_hmacek = reply[1][len('rsapms_hmacms_hmacek:'):]
    try: rsapms_hmacms_hmacek = b64decode(b64_rsapms_hmacms_hmacek)    
    except: return ('base64 decode error in rsapms_hmacms_hmacek')
  
    RSA_PMS2 = rsapms_hmacms_hmacek[:modulus_len_int]
    RSA_PMS2_int = ba2int(RSA_PMS2)
    shahmac2_for_MS = rsapms_hmacms_hmacek[modulus_len_int:modulus_len_int+24]
    if cipher_suite == 'AES256': 
        md5hmac_for_ek = rsapms_hmacms_hmacek[modulus_len_int+24:modulus_len_int+24+136]
    elif cipher_suite == 'AES128': 
        md5hmac_for_ek = rsapms_hmacms_hmacek[modulus_len_int+24:modulus_len_int+24+104]
    elif cipher_suite == 'RC4SHA': 
        md5hmac_for_ek = rsapms_hmacms_hmacek[modulus_len_int+24:modulus_len_int+24+72]
    elif cipher_suite == 'RC4MD5': 
        md5hmac_for_ek = rsapms_hmacms_hmacek[modulus_len_int+24:modulus_len_int+24+64]       
    #RSA encryption without padding: ciphertext = plaintext^e mod n
    RSA_PMS1_int = pow(ba2int(('\x02'+('\x01'*(modulus_len_int - 100))+'\x00'+
                                PMS1+('\x00'*24))) + 1, exponent_int, modulus_int)
    enc_pms_int = (RSA_PMS2_int*RSA_PMS1_int) % modulus_int 
    enc_pms = bigint_to_bytearray(enc_pms_int)
    with open(join(nss_patch_dir, 'encpms'+uid), 'wb') as f: f.write(enc_pms)
    with open(join(nss_patch_dir, 'encpms'+uid+'ready' ), 'wb') as f: f.close()   
    #master secret key expansion
    MS2 = xor(md5hmac2_for_MS, shahmac2_for_MS)        
    #see RFC2246 6.3. Key calculation & 5. HMAC and the pseudorandom function
    #The amount of key material for each ciphersuite:
    #AES256-CBC-SHA: mac key 20*2, encryption key 32*2, IV 16*2 == 136bytes
    #AES128-CBC-SHA: mac key 20*2, encryption key 16*2, IV 16*2 == 104bytes
    #RC4128_SHA: mac key 20*2, encryption key 16*2 == 72bytes
    #RC4128_MD5: mac key 16*2, encryption key 16*2 == 64 bytes
    #Regardless of theciphersuite, we generate the max key material we'd ever need which is 136 bytes
    label = 'key expansion'
    seed = sr + cr
    #this is not optimized in a loop on purpose. I want people to see exactly what is going on   
    sha1A1 = hmac.new(MS2, label+seed, sha1).digest()
    sha1A2 = hmac.new(MS2, sha1A1, sha1).digest()
    sha1A3 = hmac.new(MS2, sha1A2, sha1).digest()
    sha1A4 = hmac.new(MS2, sha1A3, sha1).digest()
    sha1A5 = hmac.new(MS2, sha1A4, sha1).digest()
    sha1A6 = hmac.new(MS2, sha1A5, sha1).digest()
    sha1A7 = hmac.new(MS2, sha1A6, sha1).digest()   
    sha1hmac1 = hmac.new(MS2, sha1A1 + label + seed, sha1).digest()
    sha1hmac2 = hmac.new(MS2, sha1A2 + label + seed, sha1).digest()
    sha1hmac3 = hmac.new(MS2, sha1A3 + label + seed, sha1).digest()
    sha1hmac4 = hmac.new(MS2, sha1A4 + label + seed, sha1).digest()
    sha1hmac5 = hmac.new(MS2, sha1A5 + label + seed, sha1).digest()
    sha1hmac6 = hmac.new(MS2, sha1A6 + label + seed, sha1).digest()
    sha1hmac7 = hmac.new(MS2, sha1A7 + label + seed, sha1).digest()    
    sha1hmac140bytes = sha1hmac1+sha1hmac2+sha1hmac3+sha1hmac4+sha1hmac5+sha1hmac6+sha1hmac7
    #this if/else is purely for expliciteness, we could simply xor the 140bytes with however long the md5hmac is
    if cipher_suite == 'AES256': sha1hmac_for_ek = sha1hmac140bytes[:136]
    elif cipher_suite == 'AES128': sha1hmac_for_ek = sha1hmac140bytes[:104]
    elif cipher_suite == 'RC4SHA': sha1hmac_for_ek = sha1hmac140bytes[:72]
    elif cipher_suite == 'RC4MD5': sha1hmac_for_ek = sha1hmac140bytes[:64]     
    expanded_keys =  xor(sha1hmac_for_ek, md5hmac_for_ek)     
    #server mac key == expanded_keys[20:40]( or [16:32] for RC4MD5) contains random garbage from auditor
    with open(join(nss_patch_dir, 'expanded_keys'+uid), 'wb') as f: f.write(expanded_keys)
    with open(join(nss_patch_dir, 'expanded_keys'+uid+'ready'), 'wb') as f: f.close()     
    #wait for nss patch to create md5 and then sha files
    while True:
        if not os.path.isfile(join(nss_patch_dir, 'sha'+uid)):
            time.sleep(0.1)
        else:
            time.sleep(0.1)
            break  
    with open(join(nss_patch_dir, 'md5'+uid), 'rb') as f: md5_digest = f.read()
    with open(join(nss_patch_dir, 'sha'+uid), 'rb') as f: sha_digest = f.read()
    
    b64_verify_md5sha = b64encode(md5_digest+sha_digest)
    reply = send_and_recv('verify_md5sha:'+b64_verify_md5sha)
    if reply[0] != 'success': return ('Failed to receive a reply')
    if not reply[1].startswith('verify_hmac:'): return ('bad reply. Expected verify_hmac:')
    b64_verify_hmac = reply[1][len('verify_hmac:'):]
    try: verify_hmac = b64decode(b64_verify_hmac)    
    except: return ('base64 decode error')    
    #calculate verify_data for Finished message
    #see RFC2246 7.4.9. Finished & 5. HMAC and the pseudorandom function
    label = 'client finished'
    seed = md5_digest + sha_digest
    sha1A1 = hmac.new(MS2, label+seed, sha1).digest()
    sha1hmac1 = hmac.new(MS2, sha1A1 + label + seed, sha1).digest()
    verify_data = xor(verify_hmac, sha1hmac1)[:12]   
    with open(join(nss_patch_dir, 'verify_data'+uid), 'wb') as f: f.write(bytearray(verify_data))
    with open(join(nss_patch_dir, 'verify_data'+uid+'ready'), 'wb') as f: f.close()
    return 'success'
    
   
#scan the dir until a new file appears and then spawn a new processing thread
def nss_patch_audited_connections():
    uidsAlreadyProcessed = []
    uid = ''
    bNewUIDFound = False    
    while True:
        time.sleep(0.1)
        files = os.listdir(nss_patch_dir)
        for onefile in files:
            #patch creates files: der*,cr*, and sr*. Proceed when sr* was created
            if not onefile.startswith('sr'): continue
            if onefile in uidsAlreadyProcessed: continue
            uid =onefile[2:]
            uidsAlreadyProcessed.append(onefile)
            bNewUIDFound = True
            break
        if bNewUIDFound == False: continue
        #else if new uid found
        rv = new_audited_connection(uid)
        bNewUIDFound = False            
        if rv != 'success':
            print ('Error occured while processing nss patch dir:' + rv)
            break


def new_connection_thread(socket_client, new_address):
    #extract destination address from the http header
    #the header has a form of: CONNECT encrypted.google.com:443 HTTP/1.1 some_other_stuff
    headers_str = socket_client.recv(8192)
    headers = headers_str.split()
    if len(headers) < 2:
        print ('Invalid or empty header received: ' + headers_str)
        socket_client.close()
        return
    if headers[0] != 'CONNECT':
        print ('Expected CONNECT in header but got ' + headers[0] + '. Please investigate')
        socket_client.close()
        return
    if headers[1].find(':') == -1:
        print ('Expected colon in the address part of the header but none found. Please investigate')
        socket_client.close()
        return
    split_result = headers[1].split(':')
    if len(split_result) != 2:
        print ('Expected only two values after splitting the header. Please investigate')
        socket_client.close()
        return
    host, port = split_result
    try: int_port = int(port)
    except:
        print ('Port is not a numerical value. Please investigate')
        socket_client.close()
        return
    try: host_ip = socket.gethostbyname(host)
    except: #happens when IP lookup fails for some IP6-only hosts
        socket_client.close()
        return
    socket_target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socket_target.connect((host_ip, int_port))
    print ('New connection to ' + host_ip + ' port ' + port)
    #tell Firefox that connection is established and it can start sending data
    socket_client.send('HTTP/1.1 200 Connection established\n' + 'Proxy-agent: tlsnotary https proxy\n\n')
    
    last_time_data_was_seen = int(time.time())
    while True:
        rlist, wlist, xlist = select.select((socket_client, socket_target), (), (socket_client, socket_target), 60)
        if len(rlist) == len(wlist) == len(xlist) == 0: #timeout
            print ('Socket 60 second timeout. Terminating connection')
            socket_client.close()
            socket_target.close()
            return
        if len(xlist) > 0:
            print ('Socket exceptional condition. Terminating connection')
            socket_client.close()
            socket_target.close()
            return
        if len(rlist) == 0:
            print ('Python internal socket error: rlist should not be empty. Please investigate. Terminating connection')
            socket_client.close()
            socket_target.close()
            return
        #else rlist contains socket with data
        for rsocket in rlist:
            try:
                data = rsocket.recv(8192)
                if not data: 
                    #XXX Why did select() trigger if there was no data?
                    #this overwhelms CPU big time unless we sleep
                    if int(time.time()) - last_time_data_was_seen > 60: #prevent no-data sockets from looping endlessly
                        socket_client.close()
                        socket_target.close()
                        return
                    #else timeout seconds of datalessness have not elapsed
                    time.sleep(0.1)
                    continue 
                last_time_data_was_seen = int(time.time())
                if rsocket is socket_client:
                    socket_target.send(data)
                    continue
                elif rsocket is socket_target:
                    socket_client.send(data)
                    continue
            except Exception, e:
                print (e)
                socket_client.close()
                socket_target.close()
                return
         
        
def https_proxy_thread(parenthread, port):
    socket_proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try: socket_proxy.bind(('localhost', port))
    except: #socket is in use
        parenthread.retval = 'failure'
        return
    parenthread.retval = 'success'    
    print ('HTTPS proxy is serving on port ' + str(port))
    socket_proxy.listen(5) #5 requests can be queued
    while True:
        #block until a new connection appears
        print ('listening for a new connection')
        new_socket, new_address = socket_proxy.accept()
        print ('new connection  accepted')
        thread = threading.Thread(target= new_connection_thread, args=(new_socket, new_address))
        thread.daemon = True
        thread.start()
        

def start_recording():
    global stcppipe_proc
    global stcppipe_pid
   
    #start the https proxy and make sure the port is not in use
    bWasStarted = False
    for i in range(3):
        HTTPS_proxy_port =  random.randint(1025,65535)
        thread = ThreadWithRetval(target= https_proxy_thread, args=(HTTPS_proxy_port, ))
        thread.daemon = True
        thread.start()
        time.sleep(1)
        if thread.retval != 'success': continue
        bWasStarted = True
        break
    if bWasStarted == False: return ('failure to start HTTPS proxy')
    print ('Started HTTPS proxy on port ' + str(HTTPS_proxy_port))
    #start stcppipe making sure the port is not in use
    bWasStarted = False
    logdir = join(current_sessiondir, 'tracelog')
    os.makedirs(logdir)
    if OS=='mswin': stcppipe_exename = 'stcppipe.exe'
    elif OS=='linux': 
        if platform.architecture()[0] == '64bit': stcppipe_exename = 'stcppipe64_linux'
        else: stcppipe_exename = 'stcppipe_linux'
    elif OS=='macos': 
        if platform.architecture()[0] == '64bit':stcppipe_exename = 'stcppipe64_mac'
        else: stcppipe_exename = 'stcppipe_mac'                
    for i in range(3):
        FF_proxy_port = random.randint(1025,65535)     
        stcppipe_proc = Popen([join(datadir, 'stcppipe', stcppipe_exename),'-d',
                               logdir, '-b', '127.0.0.1', str(HTTPS_proxy_port), str(FF_proxy_port)])
        time.sleep(1)
        if stcppipe_proc.poll() != None:
            print ('Maybe the port was in use, trying again with a new port')
            continue
        else:
            bWasStarted = True
            break
    if bWasStarted == False: return ('failure to start stcppipe')
    print ('stcppipe is piping from port ' + str(FF_proxy_port) + ' to port ' + str(HTTPS_proxy_port))
    stcppipe_pid = stcppipe_proc.pid 
    thread = threading.Thread(target= nss_patch_audited_connections)
    thread.daemon = True
    thread.start()
    return ('success', FF_proxy_port)


#respond to PING messages and put all the other messages onto the recvQueue
def receivingThread(my_nick, auditor_nick, IRCsocket):
    if not hasattr(receivingThread, 'last_seq_which_i_acked'):
        receivingThread.last_seq_which_i_acked = 100000 #static variable. Initialized only on first function's run
    chunks = []
    while not bReceivingThreadStopFlagIsSet:
        buffer = ''
        try: buffer = IRCsocket.recv(1024)
        except: continue #1 sec timeout
        if not buffer: continue
        #sometimes the IRC server may pack multiple PRIVMSGs into one message separated with /r/n/
        messages = buffer.split('\r\n')
        for onemsg in messages:
            msg = onemsg.split()
            if len(msg) == 0: continue  #stray newline
            if msg[0] == 'PING': #check if server have sent ping command
                IRCsocket.send('PONG %s' % msg[1]) #answer with pong as per RFC 1459
                continue
            if not len(msg) >= 5: continue
            if not (msg[1] == 'PRIVMSG' and msg[2] == channel_name and msg[3] == ':'+my_nick ): continue
            exclamaitionMarkPosition = msg[0].find('!')
            nick_from_message = msg[0][1:exclamaitionMarkPosition]
            if not auditor_nick == nick_from_message: continue
            print ('RECEIVED:' + buffer)
            if len(msg)==5 and msg[4].startswith('ack:'):
                ackQueue.put(msg[4][len('ack:'):])
                continue
            if not (len(msg)==7 and msg[4].startswith('seq:')): continue
            his_seq = int(msg[4][len('seq:'):])
            if his_seq <=  receivingThread.last_seq_which_i_acked: 
                #the other side is out of sync, send an ack again
                IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditor_nick + ' ack:' + str(his_seq) + ' \r\n')
                continue
            if not his_seq == receivingThread.last_seq_which_i_acked +1: continue #we did not receive the next seq in order
            #else we got a new seq      
            if len(chunks)==0 and not msg[5].startswith((
                'grsapms_ghmac', 'rsapms_hmacms_hmacek', 'verify_hmac:', 'logsig:', 'response:', 'sha1hmac_for_MS')) : continue         
            #'CRLF' is used at the end of the first chunk, 'EOL' is used to show that there are no more chunks
            chunks.append(msg[5])
            IRCsocket.send('PRIVMSG ' + channel_name + ' :' + auditor_nick + ' ack:' + str(his_seq) + ' \r\n')
            receivingThread.last_seq_which_i_acked = his_seq            
            if msg[-1]=='EOL':
                assembled_message = ''.join(chunks)
                recvQueue.put(assembled_message)              
                chunks = []
               

def start_irc():
    global my_nick
    global auditor_nick
    global IRCsocket
    global google_modulus
    global google_exponent
    
    my_nick= 'user' + ''.join(random.choice('0123456789') for x in range(10))  
    IRCsocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    IRCsocket.connect(('chat.freenode.net', 6667))
    IRCsocket.send('USER %s %s %s %s' % ('these', 'arguments', 'are', 'optional') + '\r\n')
    IRCsocket.send('NICK ' + my_nick + '\r\n')  
    IRCsocket.send('JOIN %s' % channel_name + '\r\n')  
    # ----------------------------------BEGIN get the certficate for google.com and extract modulus/exponent
    tlssock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tlssock.settimeout(10)
    tlssock.connect(('google.com', 443))
    cr_time = bigint_to_bytearray(int(time.time()))
    cr_google = cr_time + os.urandom(28)
    client_hello = '\x16\x03\x01\x00\x2d\x01\x00\x00\x29\x03\x01' + cr_google + '\x00\x00\x02\x00\x35\x01\x00'
    tlssock.send(client_hello)
    time.sleep(1)
    serverhello_certificate_serverhellodone = tlssock.recv(8192*2)    #google sends a ridiculously long cert chain of 10KB+
    #server hello starts with 16 03 01 * * 02
    #certificate starts with 16 03 01 * * 0b
    serverhellodone = '\x16\x03\x01\x00\x04\x0e\x00\x00\x00'
    
    if not re.match(re.compile(b'\x16\x03\x01..\x02'), serverhello_certificate_serverhellodone):
        print ('Invalid server hello from google')
        return 'failure'
    if not serverhello_certificate_serverhellodone.endswith(serverhellodone):
        print ('invalid server hello done from google')
        return 'failure'
    #find the beginning of certificate message
    cert_match = re.search(re.compile(b'\x16\x03\x01..\x0b'), serverhello_certificate_serverhellodone)
    if not cert_match:
        print ('Invalid certificate message from google')
        return 'failure'
    cert_start_position = cert_match.start()
    certificate = serverhello_certificate_serverhellodone[cert_start_position : -len(serverhellodone)]
    #extract modulus and exponent from the certificate
    cert_len = ba2int(certificate[12:15])
    google_cert = certificate[15:15+cert_len]
    try:       
        rv  = decoder.decode(google_cert, asn1Spec=univ.Sequence())
        bitstring = rv[0].getComponentByPosition(0).getComponentByPosition(6).getComponentByPosition(1)
        #bitstring is a list of ints, like [01110001010101000...]
        #convert it into into a string   '01110001010101000...'
        stringOfBits = ''
        for bit in bitstring:
            bit_as_str = str(bit)
            stringOfBits += bit_as_str    
        #treat every 8 chars as an int and pack the ints into a bytearray
        ba = bytearray()
        for i in range(0, len(stringOfBits)/8):
            onebyte = stringOfBits[i*8 : (i+1)*8]
            oneint = int(onebyte, base=2)
            ba.append(oneint)       
        #decoding the nested sequence
        rv  = decoder.decode(str(ba), asn1Spec=univ.Sequence())
        exponent = rv[0].getComponentByPosition(1)
        modulus = rv[0].getComponentByPosition(0)
        google_modulus = int(modulus)
        google_exponent = int(exponent)
        google_n = bigint_to_bytearray(google_modulus)
        google_e = bigint_to_bytearray(google_exponent)
    except:
        print ('Error decoding der pubkey from google')
        return 'failure'
    # ----------------------------------END get the certficate for google.com and extract modulus/exponent
    modulus = bigint_to_bytearray(auditorPubKey.n)[:10]
    signed_hello = rsa.sign('client_hello', myPrvKey, 'SHA-1')
    b64_hello = b64encode(modulus+signed_hello)
    b64_google_pubkey = b64encode(google_n+google_e)
    #hello contains the first 10 bytes of modulus of the auditor's pubkey
    #this is how the auditor knows on IRC that we are addressing him.
    IRCsocket.settimeout(1)
    bIsAuditorRegistered = False
    for attempt in range(6): #try for 6*10 secs to find the auditor
        if bIsAuditorRegistered == True: break #previous iteration successfully regd the auditor
        time_attempt_began = int(time.time())
        IRCsocket.send('PRIVMSG ' + channel_name + ' :client_hello:'+b64_hello +' \r\n')
        time.sleep(1)
        IRCsocket.send('PRIVMSG ' + channel_name + ' :google_pubkey:'+b64_google_pubkey +' \r\n')
        while not bIsAuditorRegistered:
            if int(time.time()) - time_attempt_began > 10: break
            buffer = ''
            try: buffer = IRCsocket.recv(1024)
            except: continue #1 sec timeout
            if not buffer: continue
            print (buffer)
            messages = buffer.split('\r\n')  #sometimes the IRC server may pack multiple PRIVMSGs into one message separated with /r/n/
            for onemsg in messages:
                msg = onemsg.split()
                if len(msg)==0 : continue  #stray newline
                if msg[0] == 'PING':
                    IRCsocket.send('PONG %s' % msg[1]) #answer with pong as per RFC 1459
                    continue
                if not len(msg) == 5: continue
                if not (msg[1]=='PRIVMSG' and msg[2]==channel_name and msg[3]==':'+my_nick and msg[4].startswith('server_hello:')): continue
                b64_signed_hello = msg[4][len('server_hello:'):]
                try:
                    signed_hello = b64decode(b64_signed_hello)
                    rsa.verify('server_hello', signed_hello, auditorPubKey)
                    #if no exception:
                    exclamaitionMarkPosition = msg[0].find('!')
                    auditor_nick = msg[0][1:exclamaitionMarkPosition]
                    bIsAuditorRegistered = True
                    print ('Auditor successfully verified')
                    break
                except:
                    print ('hello verification failed. Are you sure you have the correct auditor\'s pubkey?')
                    continue
    if not bIsAuditorRegistered:
        print ('Failed to register auditor within 60 seconds')
        return 'failure'
    
    thread = threading.Thread(target= receivingThread, args=(my_nick, auditor_nick, IRCsocket))
    thread.daemon = True
    thread.start()
    return 'success'
    
    
    
def start_firefox(FF_to_backend_port):    
    if OS=='linux':
        firefox_exepath = join(datadir, 'firefoxcopy', 'firefox')
        if not os.path.exists(firefox_exepath): raise Exception('firefox missing')
    if OS=='mswin':
        firefox_exepath = join(datadir, 'firefoxcopy', 'firefox.exe')
        if not os.path.exists(firefox_exepath): raise Exception('firefox missing')
    if OS=='macos':
        firefox_exepath = join(datadir, 'firefoxcopy', 'Contents', 'MacOS', 
                                       'TorBrowser.app', 'Contents', 'MacOS', 'firefox')
        if not os.path.exists(firefox_exepath): raise Exception('firefox missing')  
    import stat
    os.chmod(firefox_exepath,stat.S_IRWXU)
    logs_dir = join(datadir, 'logs')
    if not os.path.isdir(logs_dir): os.makedirs(logs_dir)
    with open(join(logs_dir, 'firefox.stdout'), 'w') as f: pass
    with open(join(logs_dir, 'firefox.stderr'), 'w') as f: pass
    ffprof_dir = join(datadir, 'FF-profile')
    if not os.path.exists(ffprof_dir): os.makedirs(ffprof_dir)
    #show addon bar
    with codecs.open(join(ffprof_dir, 'localstore.rdf'), 'w') as f:
        f.write('<?xml version="1.0"?>'
                '<RDF:RDF xmlns:NC="http://home.netscape.com/NC-rdf#" xmlns:RDF="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                '<RDF:Description RDF:about="chrome://browser/content/browser.xul">'
                '<NC:persist RDF:resource="chrome://browser/content/browser.xul#addon-bar" collapsed="false"/>'
                '</RDF:Description></RDF:RDF>')        
    bundles_dir = join(os.path.dirname(firefox_exepath), 'distribution', 'bundles')
    if not os.path.exists(bundles_dir):
        os.makedirs(bundles_dir)
        addons = os.listdir(join(datadir, 'FF-addon'))
        for oneaddon in addons:
            shutil.copytree(join(datadir, 'FF-addon', oneaddon), 
                            join(bundles_dir, oneaddon))
    with open(join(ffprof_dir, 'prefs.js'), 'w') as f:
        f.writelines([
        'user_pref("browser.startup.homepage", "chrome://tlsnotary/content/auditee.html");\n',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n', #prevents welcome page
        'user_pref("browser.rights.3.shown", true);\n', 
        'user_pref("app.update.auto", false);\n',
        'user_pref("app.update.enabled", false);\n',
        'user_pref("browser.shell.checkDefaultBrowser", false);\n',
        'user_pref("browser.search.update", false);\n',
        'user_pref("browser.link.open_newwindow", 3);\n', #open new window in a new tab
        'user_pref("browser.link.open_newwindow.restriction", 0);\n', #enforce the above rule without exceptions
        'user_pref("extensions.lastAppVersion", "100.0.0");\n',
        'user_pref("extensions.checkCompatibility.4.*", false);\n',
        'user_pref("extensions.update.autoUpdate", false);\n',
        'user_pref("extensions.update.enabled", false);\n',
        'user_pref("extensions.enabledScopes", 0);\n', #prevent from looking for system addons
        'user_pref("datareporting.healthreport.service.enabled", false);\n',
        'user_pref("datareporting.healthreport.uploadEnabled", false);\n',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
		'user_pref("gfx.direct2d.disabled", true);\n'
		'user_pref("layers.acceleration.disabled", true);\n'
        'user_pref("browser.sessionstore.resume_from_crash", false);\n'
        ])        
    os.putenv('FF_to_backend_port', str(FF_to_backend_port))
    os.putenv('FF_first_window', 'true')   #prevents addon confusion when websites open multiple FF windows
    #keep trailing slash to tell the patch which path delimiter to use (nix vs win)
    os.putenv('NSS_PATCH_DIR', join(nss_patch_dir, ''))
    
    print ('Starting a new instance of Firefox with tlsnotary profile',end='\r\n')
    try: ff_proc = Popen([firefox_exepath,'-no-remote', '-profile', ffprof_dir],
                                   stdout=open(join(logs_dir, 'firefox.stdout'),'w'), 
                                   stderr=open(join(logs_dir, 'firefox.stderr'), 'w'))
    except Exception,e: return ('Error starting Firefox: %s' %e,)
    return ('success', ff_proc)


class StoppableHttpServer (BaseHTTPServer.HTTPServer):
    """http server that reacts to self.stop flag"""
    retval = ''
    def serve_forever (self):
        """Handle one request at a time until stopped. Optionally return a value"""
        self.stop = False
        while not self.stop:
                self.handle_request()
        return self.retval;
    

#HTTP server to talk with Firefox addon
def http_server(parentthread):    
    #allow three attempts in case if the port is in use
    bWasStarted = False
    for i in range(3):
        FF_to_backend_port = random.randint(1025,65535)
        print ('Starting http server to communicate with Firefox addon')
        try:
            httpd = StoppableHttpServer(('127.0.0.1', FF_to_backend_port), HandlerClass)
            bWasStarted = True
            break
        except Exception, e:
            print ('Error starting mini http server. Maybe the port is in use?', e,end='\r\n')
            continue
    if bWasStarted == False:
        #retval is a var that belongs to our parent class which is ThreadWithRetval
        parentthread.retval = ('failure',)
        return
    #Let the invoking thread know that we started successfully
    parentthread.retval = ('success', FF_to_backend_port)
    sa = httpd.socket.getsockname()
    print ('Serving HTTP on', sa[0], 'port', sa[1], '...',end='\r\n')
    httpd.serve_forever()
    return
    
        
def quit(sig=0, frame=0):
    if stcppipe_pid != 0:
        try: os.kill(stcppipe_pid, signal.SIGTERM)
        except: pass #stcppipe not runnng
    if firefox_pid != 0:
        try: os.kill(firefox_pid, signal.SIGTERM)
        except: pass #firefox not runnng
    if selftest_pid != 0:
        try: os.kill(selftest_pid, signal.SIGTERM)
        except: pass #selftest not runnng    
    exit(1)
    
 
def first_run_check():
    #On first run, extract rsa,pyasn1,firefox and check hashes
    rsa_dir = join(datadir, 'python', 'rsa-3.1.4')
    if not os.path.exists(rsa_dir):
        print ('Extracting rsa-3.1.4.tar.gz...')
        with open(join(datadir, 'python', 'rsa-3.1.4.tar.gz'), 'rb') as f: tarfile_data = f.read()
        #for md5 hash, see https://pypi.python.org/pypi/rsa/3.1.4
        if md5(tarfile_data).hexdigest() != 'b6b1c80e1931d4eba8538fd5d4de1355':
            raise Exception ('Wrong hash')
        os.chdir(join(datadir, 'python'))
        tar = tarfile.open(join(datadir, 'python', 'rsa-3.1.4.tar.gz'), 'r:gz')
        tar.extractall()
        tar.close()
      
    pyasn1_dir = join(datadir, 'python', 'pyasn1-0.1.7')
    if not os.path.exists(pyasn1_dir):
        print ('Extracting pyasn1-0.1.7.tar.gz...')
        with open(join(datadir, 'python', 'pyasn1-0.1.7.tar.gz'), 'rb') as f: tarfile_data = f.read()
        #for md5 hash, see https://pypi.python.org/pypi/pyasn1/0.1.7
        if md5(tarfile_data).hexdigest() != '2cbd80fcd4c7b1c82180d3d76fee18c8':
            raise Exception ('Wrong hash')
        os.chdir(join(datadir, 'python'))
        tar = tarfile.open(join(datadir, 'python', 'pyasn1-0.1.7.tar.gz'), 'r:gz')
        tar.extractall()
        tar.close()
        
    requests_dir = join(datadir, 'python', 'requests-2.3.0')
    if not os.path.exists(requests_dir):
        print ('Extracting requests-2.3.0.tar.gz...')
        with open(join(datadir, 'python', 'requests-2.3.0.tar.gz'), 'rb') as f: tarfile_data = f.read()
        #for md5 hash, see https://pypi.python.org/pypi/requests/2.3.0
        if md5(tarfile_data).hexdigest() != '7449ffdc8ec9ac37bbcd286003c80f00':
            raise Exception ('Wrong hash')
        os.chdir(join(datadir, 'python'))
        tar = tarfile.open(join(datadir, 'python', 'requests-2.3.0.tar.gz'), 'r:gz')
        tar.extractall()
        tar.close()    
    
    if not os.path.exists(join(datadir, 'firefoxcopy')):
        print ('Extracting Firefox ...')
        if OS=='linux':
            #github doesn't allow to upload .tar.xz, so we add extension now
            zipname = 'firefox-linux'
            zipname += '64' if platform.machine() == 'x86_64' else '32'
            fullpath = join(installdir, zipname)
            if os.path.exists(fullpath): 
                os.rename(fullpath, fullpath + '.tar.xz')
            elif not os.path.exists(fullpath + '.tar.xz'):
                raise Exception ('Couldn\'t find either '+zipname+' or '+ 
                                 zipname+'.tar.xz in '+installdir)
            browser_zip_path = fullpath + '.tar.xz'              
            try:
                check_output(['xz', '-d', '-k', browser_zip_path]) #extract and keep the sourcefile
            except:
                raise Exception ('Could not extract ' + browser_zip_path +
                                 '.Make sure xz is installed on your system')
            #The result of the extraction will be firefox-linux*.tar
            tarball_path = join(installdir, 'firefox-linux')
            tarball_path += '64.tar' if platform.machine() == 'x86_64' else '32.tar'
            m_tarfile = tarfile.open(tarball_path)
            #tarball extracts into current working dir
            os.makedirs(join(datadir, 'tmpextract'))
            os.chdir(join(datadir, 'tmpextract'))
            m_tarfile.extractall()
            m_tarfile.close()
            os.remove(tarball_path)
            #change working dir away from the deleted one, otherwise FF will not start
            os.chdir(datadir)
            source_dir = join(datadir, 'tmpextract', 'tor-browser_en-US', 'Browser')
            shutil.copytree(source_dir, join(datadir, 'firefoxcopy'))
            shutil.rmtree(join(datadir, 'tmpextract'))
            
        if OS=='mswin':
            exename = 'firefox-windows'
            installer_exe_path = join(installdir, exename)
            if not os.path.exists(installer_exe_path):
                raise Exception ('Couldn\'t find '+exename+'  in '+installdir)
            os.chdir(installdir) #installer silently extract into the current working dir XXX: do we need this line?
            installer_proc = Popen(installer_exe_path + ' /S' + ' /D='+join(datadir, 'tmpextract')) #silently extract into destination
            bInstallerFinished = False
            for i in range(30): #give the installer 30 secs to extract the files and exit
                time.sleep(1)                
                if installer_proc.poll() == None: continue
                #else
                bInstallerFinished = True
                break
            if not bInstallerFinished:
                raise Exception ('Installer took too long to extract files')
            #Copy the extracted files and delete them to keep datadir organized
            source_dir = join(datadir, 'tmpextract', 'Browser')
            shutil.copytree(source_dir, join(datadir, 'firefoxcopy'))
            shutil.rmtree(join(datadir, 'tmpextract'))
               
        if OS=='macos':
            zipname = 'firefox-macosx'
            if os.path.exists(join(installdir, zipname)):
                browser_zip_path = join(installdir, zipname)
            else:
                raise Exception ('Couldn\'t find '+zipname+' in '+installdir)
            m_zipfile = zipfile.ZipFile(browser_zip_path, 'r')
            m_zipfile.extractall(join(datadir, 'tmpextract'))
            #files get extracted in a root dir Browser
            source_dir = join(datadir, 'tmpextract', 'TorBrowserBundle_en-US.app')
            shutil.copytree(source_dir, join(datadir, 'firefoxcopy'))
            shutil.rmtree(join(datadir, 'tmpextract'))    
    
    
    
if __name__ == "__main__":
    first_run_check()
    sys.path.append(join(datadir, 'python', 'rsa-3.1.4'))
    sys.path.append(join(datadir, 'python', 'pyasn1-0.1.7'))
    sys.path.append(join(datadir, 'python', 'slowaes'))
    sys.path.append(join(datadir, 'python', 'requests-2.3.0'))    
    import rsa
    import pyasn1
    import requests
    from pyasn1.type import univ
    from pyasn1.codec.der import encoder, decoder
    from slowaes import AESModeOfOperation        
    
    if OS=='linux': tshark_exepath = 'tshark'
    elif OS=='mswin':
        tshark64 = join(os.getenv('ProgramW6432'), 'Wireshark',  'tshark.exe' )
        if os.path.isfile(tshark64): tshark_exepath = tshark64
        tshark32 = join(os.getenv('ProgramFiles(x86)'), 'Wireshark',  'tshark.exe' )
        if os.path.isfile(tshark32): tshark_exepath = tshark32
        if tshark_exepath == '' : raise Exception(
            'Failed to find wireshark in your Program Files', end='\r\n')
    elif OS=='macos':
        tshark_osx = '/Applications/Wireshark.app/Contents/Resources/bin/tshark'
        if os.path.isfile(tshark_osx): tshark_exepath = tshark_osx
        else: raise  Exception('Failed to find wireshark in your Applications folder', end='\r\n')    
      
    thread = ThreadWithRetval(target= http_server)
    thread.daemon = True
    thread.start()
    #wait for minihttpd thread to indicate its status and FF_to_backend_port  
    bWasStarted = False
    for i in range(10):
        time.sleep(1)        
        if thread.retval == '': continue
        #else
        if thread.retval[0] != 'success': raise Exception (
            'Failed to start minihttpd server. Please investigate')
        #else
        bWasStarted = True
        break
    if bWasStarted == False:
        raise Exception ('minihttpd failed to start in 10 secs. Please investigate')
    FF_to_backend_port = thread.retval[1]
        
    ff_retval = start_firefox(FF_to_backend_port)
    if ff_retval[0] != 'success': raise Exception (
        'Error while starting Firefox: '+ ff_retval[0], end='\r\n')
    ff_proc = ff_retval[1]
    firefox_pid = ff_proc.pid    
    
    signal.signal(signal.SIGTERM, quit)
    try:
        while True:
            time.sleep(1)
            if ff_proc.poll() != None: quit() #FF was closed
    except KeyboardInterrupt: quit()            
