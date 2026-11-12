
const util = require('minecraft-server-util');

// 获取命令行参数
const serverIp = process.argv[2];
const serverPort = parseInt(process.argv[3]);

util.status(serverIp, serverPort)
    .then(result => {
        console.log(JSON.stringify(result));
    })
    .catch(error => {
        console.error(JSON.stringify({error: error.message}));
        process.exit(1);
    });
