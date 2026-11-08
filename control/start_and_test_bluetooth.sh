# start bluetooth '
sudo systemctl restart bluetooth
sleep 5
sudo bluetoothctl power on

# if it’s still busy, see what’s going on:'

rfkill list
bluetoothctl show | head -8
grep -n -i "bt\|bluetooth" /boot/firmware/config.txt
sudo systemctl status hciuart --no-pager | head -5
