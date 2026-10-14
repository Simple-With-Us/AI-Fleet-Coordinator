"""Manual one-shot synthetic test only. Never called by the service."""
import os, smtplib, ssl, sys, time
from pathlib import Path
from email.message import EmailMessage

def main():
    seat = sys.argv[1]
    dest = {'echo':'jaywedgeworth@mail.instinct.com','wise':'wedgeworth@mail.instinct.com'}[seat]
    marker = '/data/smtp-test-'+seat+'-20261013.attempt'
    fd = os.open(marker, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
    os.write(fd,b'attempted\n'); os.fsync(fd); os.close(fd)
    msg = EmailMessage()
    msg['From'] = os.environ['RELAY_SMTP_USERNAME']; msg['To'] = dest
    msg['Subject'] = 'ECHO relay SMTP test'
    msg['Message-ID'] = '<echo-relay-smtp-test-'+seat+'-20261013@relay.invalid>'
    msg.set_content('ECHO relay SMTP test.  Synthetic event only; no Zulip message content.  Normal event delivery is still disabled.')
    try:
        with smtplib.SMTP('smtp.gmail.com',587,timeout=20) as smtp:
            smtp.ehlo(); smtp.starttls(context=ssl.create_default_context()); smtp.ehlo()
            smtp.login(os.environ['RELAY_SMTP_USERNAME'],os.environ['RELAY_SMTP_PASSWORD'])
            if smtp.send_message(msg): raise RuntimeError('recipient_refused')
        Path('/data/smtp-test-'+seat+'-20261013.result').write_text('accepted '+str(msg['Message-ID']))
        print('SMTP accepted',seat,str(msg['Message-ID']),int(time.time()))
    except Exception as error:
        Path('/data/smtp-test-'+seat+'-20261013.result').write_text('unconfirmed '+type(error).__name__)
        print('SMTP test not confirmed; inspect before any retry',seat)
        sys.exit(1)
if __name__=='__main__':main()
